"""Order processing and execution for the TUI."""

import asyncio
import contextvars
import logging


logger = logging.getLogger(__name__)

# Context variable to track which broker is currently executing
current_broker: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_broker", default=None
)


class OrderBatchProcessor:
    """Process orders in batches for better performance."""

    def __init__(self, batch_size=5, default_broker_timeout=25):
        self.batch_size = batch_size
        self.default_broker_timeout = default_broker_timeout
        self.broker_timeouts = {
            "Chase": 45,
            "WellsFargo": 45,
        }

    def _get_broker_timeout(self, broker):
        return self.broker_timeouts.get(broker, self.default_broker_timeout)

    def _emit_status(self, status_update_fn, broker, status):
        if status_update_fn:
            status_update_fn(broker, status)

    async def _run_broker_trade(self, broker, trade_function, order):
        timeout_seconds = self._get_broker_timeout(broker)
        current_broker.set(broker)
        try:
            result = await asyncio.wait_for(
                trade_function(
                    order["action"],
                    order["quantity"],
                    order["ticker"],
                    order["price"],
                ),
                timeout=timeout_seconds,
            )
            return ("result", result)
        except asyncio.TimeoutError:
            logger.warning(
                "Broker trade timed out",
                extra={"broker": broker, "timeout_seconds": timeout_seconds},
            )
            return ("timeout", timeout_seconds)
        except Exception as exc:
            logger.exception("Broker trade raised exception", extra={"broker": broker})
            return ("error", exc)

    async def _run_validation(self, broker, validate_fn, order):
        """Run a single broker's validation with timeout."""
        try:
            return await asyncio.wait_for(
                validate_fn(
                    order["action"],
                    order["quantity"],
                    order["ticker"],
                    order["price"],
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            return (False, "Validation timed out")
        except Exception as e:
            return (False, str(e).split("\n")[0][:100])

    async def _validate_brokers(self, order, validate_functions, add_response_fn, status_update_fn):
        """Validate order against each broker that has a validate function.

        Returns:
            (validated_brokers, skipped): Lists of brokers that passed and (broker, reason) tuples that failed.
        """
        validated = []
        skipped = []

        # Split brokers into those with/without validation
        to_validate = {}
        for broker in order["selected_brokers"]:
            validate_fn = validate_functions.get(broker)
            if validate_fn:
                to_validate[broker] = validate_fn
            else:
                validated.append(broker)

        if not to_validate:
            return validated, skipped

        # Run all validations concurrently
        tasks = {
            broker: asyncio.create_task(self._run_validation(broker, fn, order))
            for broker, fn in to_validate.items()
        }
        await asyncio.gather(*tasks.values(), return_exceptions=True)

        for broker, task in tasks.items():
            try:
                result = task.result()
            except BaseException as exc:
                skipped.append((broker, str(exc).split("\n")[0][:100]))
                self._emit_status(status_update_fn, broker, "skipped")
                continue

            if result[0] is True:
                validated.append(broker)
            elif result[0] is None:
                # No credentials - let trade function handle it
                validated.append(broker)
            else:
                skipped.append((broker, result[1]))
                self._emit_status(status_update_fn, broker, "skipped")

        return validated, skipped

    async def process_orders(
        self,
        orders,
        trade_functions,
        add_response_fn,
        status_update_fn=None,
        validate_functions=None,
    ):
        """Process orders in batches."""
        total_orders = len(orders)
        successful_brokers = 0
        failed_brokers = 0
        skipped_brokers = 0
        all_broker_statuses = []

        for i in range(0, total_orders, self.batch_size):
            batch = orders[i : i + self.batch_size]
            batch_results = await self._process_batch(
                batch,
                trade_functions,
                add_response_fn,
                i + 1,
                total_orders,
                status_update_fn,
                validate_functions,
            )

            successful_brokers += batch_results["successful"]
            failed_brokers += batch_results["failed"]
            skipped_brokers += batch_results["skipped"]
            all_broker_statuses.extend(batch_results["statuses"])

        return {
            "successful": successful_brokers,
            "failed": failed_brokers,
            "skipped": skipped_brokers,
            "statuses": all_broker_statuses,
        }

    async def _process_batch(
        self,
        batch,
        trade_functions,
        add_response_fn,
        start_idx,
        total_orders,
        status_update_fn=None,
        validate_functions=None,
    ):
        """Process a batch of orders concurrently."""
        batch_successful_brokers = 0
        batch_failed_brokers = 0
        batch_skipped_brokers = 0
        batch_statuses = []

        # Create tasks for all orders in the batch
        order_tasks = []
        for idx, order in enumerate(batch, start_idx):
            order_tasks.append(
                self._process_single_order(
                    order,
                    trade_functions,
                    add_response_fn,
                    idx,
                    total_orders,
                    status_update_fn,
                    validate_functions,
                )
            )

        # Execute all order tasks concurrently
        results = await asyncio.gather(*order_tasks, return_exceptions=True)

        # Aggregate results
        for result in results:
            if isinstance(result, BaseException):
                # This shouldn't happen as _process_single_order handles its own exceptions
                logger.error(
                    "Critical error while processing order batch",
                    extra={"error": str(result)},
                )
                add_response_fn(f"❌ Critical error processing order: {result}")
                continue

            batch_successful_brokers += result["successful"]
            batch_failed_brokers += result["failed"]
            batch_skipped_brokers += result["skipped"]
            batch_statuses.append(result["status"])

        return {
            "successful": batch_successful_brokers,
            "failed": batch_failed_brokers,
            "skipped": batch_skipped_brokers,
            "statuses": batch_statuses,
        }

    async def _process_single_order(
        self,
        order,
        trade_functions,
        add_response_fn,
        idx,
        total_orders,
        status_update_fn=None,
        validate_functions=None,
    ):
        """Process a single order and return its stats."""
        progress = f"[{idx}/{total_orders}]"
        display_price = order.get("price")
        if display_price is None:
            display_price = "market"
        add_response_fn(
            f"{progress} {order['action'].upper()} {order['quantity']} {order['ticker']} @ ${display_price} via {len(order['selected_brokers'])} brokers"
        )

        broker_status = {"successful": [], "failed": [], "skipped": []}
        broker_results = []  # Collect for grouped display at end

        # Step 1: Pre-flight validation (if validate_functions provided)
        active_brokers = list(order["selected_brokers"])
        if validate_functions:
            add_response_fn(f"Validating {order['ticker']}...")
            validated, validation_skipped = await self._validate_brokers(
                order, validate_functions, add_response_fn, status_update_fn
            )

            for broker, reason in validation_skipped:
                broker_status["skipped"].append(broker)
                broker_results.append(f"   ⚠ {broker}: Skipped ({reason})")

            active_brokers = validated

            if not active_brokers:
                add_response_fn("   All brokers failed validation, skipping order")
                for msg in broker_results:
                    add_response_fn(msg)
                return {
                    "successful": 0,
                    "failed": 0,
                    "skipped": len(broker_status["skipped"]),
                    "status": broker_status,
                }

            if validation_skipped:
                add_response_fn(
                    f"Submitting to {len(active_brokers)} brokers ({len(validation_skipped)} skipped)..."
                )

        # Step 2: Execute trades on validated brokers
        broker_tasks = {}
        for broker in active_brokers:
            trade_function = trade_functions.get(broker)
            if not trade_function:
                add_response_fn(f"⚠️ {broker}: No trade function found")
                broker_status["failed"].append(f"{broker} (no function)")
                self._emit_status(status_update_fn, broker, "failed")
            else:
                # Execute broker functions as native async tasks (all broker functions are async)
                self._emit_status(status_update_fn, broker, "authing")
                broker_tasks[broker] = asyncio.create_task(
                    self._run_broker_trade(broker, trade_function, order)
                )

        # Process results as each broker completes (status bar updates immediately)
        if broker_tasks:
            pending = set()
            for broker, task in broker_tasks.items():
                task._broker_name = broker
                pending.add(task)

            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    broker = task._broker_name
                    try:
                        result = task.result()
                    except BaseException as exc:
                        broker_status["failed"].append(broker)
                        self._emit_status(status_update_fn, broker, "failed")
                        error_msg = str(exc).split("\n")[0]
                        broker_results.append(f"   ❌ {broker}: {error_msg}")
                        continue

                    if result[0] == "timeout":
                        broker_status["failed"].append(broker)
                        self._emit_status(status_update_fn, broker, "timed-out")
                        broker_results.append(
                            f"   ⏱️  {broker}: Timed out after {result[1]}s (likely reauth)"
                        )
                    elif result[0] == "error":
                        broker_status["failed"].append(broker)
                        self._emit_status(status_update_fn, broker, "failed")
                        error_msg = str(result[1]).split("\n")[0]
                        broker_results.append(f"   ❌ {broker}: {error_msg}")
                    elif result[1] is True:
                        broker_status["successful"].append(broker)
                        self._emit_status(status_update_fn, broker, "ready")
                        broker_results.append(f"   ✅ {broker}: Success")
                    elif result[1] is False:
                        broker_status["failed"].append(broker)
                        self._emit_status(status_update_fn, broker, "failed")
                        broker_results.append(f"   ❌ {broker}: Failed")
                    elif result[1] is None:
                        broker_status["skipped"].append(broker)
                        self._emit_status(status_update_fn, broker, "skipped")
                        broker_results.append(f"   ⚠️  {broker}: Skipped (no credentials)")
                    else:
                        broker_status["failed"].append(broker)
                        self._emit_status(status_update_fn, broker, "failed")
                        broker_results.append(f"   ❌ {broker}: Unknown result ({result[1]})")

        # Display all broker results together as a block
        for msg in broker_results:
            add_response_fn(msg)

        # Show per-order summary
        order_summary_parts = []
        if broker_status["successful"]:
            order_summary_parts.append(
                f"✅ {len(broker_status['successful'])} succeeded"
            )
        if broker_status["failed"]:
            order_summary_parts.append(f"❌ {len(broker_status['failed'])} failed")
        if broker_status["skipped"]:
            order_summary_parts.append(f"⚠️ {len(broker_status['skipped'])} skipped")

        if order_summary_parts:
            add_response_fn(
                f"   📊 Order {idx} results: {', '.join(order_summary_parts)}"
            )

        if idx < total_orders:
            add_response_fn("", force_redraw=True)

        return {
            "successful": len(broker_status["successful"]),
            "failed": len(broker_status["failed"]),
            "skipped": len(broker_status["skipped"]),
            "status": broker_status,
        }


# Global order batch processor
order_processor = OrderBatchProcessor()
