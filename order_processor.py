"""Order processing and execution for the TUI."""

import asyncio


class OrderBatchProcessor:
    """Process orders in batches for better performance."""

    def __init__(self, batch_size=5):
        self.batch_size = batch_size

    async def process_orders(self, orders, trade_functions, add_response_fn):
        """Process orders in batches."""
        total_orders = len(orders)
        successful_brokers = 0
        failed_brokers = 0
        skipped_brokers = 0
        all_broker_statuses = []

        for i in range(0, total_orders, self.batch_size):
            batch = orders[i:i + self.batch_size]
            batch_results = await self._process_batch(batch, trade_functions, add_response_fn, i + 1, total_orders)

            successful_brokers += batch_results["successful"]
            failed_brokers += batch_results["failed"]
            skipped_brokers += batch_results["skipped"]
            all_broker_statuses.extend(batch_results["statuses"])

        return {
            "successful": successful_brokers,
            "failed": failed_brokers,
            "skipped": skipped_brokers,
            "statuses": all_broker_statuses
        }

    async def _process_batch(self, batch, trade_functions, add_response_fn, start_idx, total_orders):
        """Process a batch of orders."""
        batch_successful_brokers = 0
        batch_failed_brokers = 0
        batch_skipped_brokers = 0
        batch_statuses = []

        for idx, order in enumerate(batch, start_idx):
            progress = f"[{idx}/{total_orders}]"
            add_response_fn(f"{progress} {order['action'].upper()} {order['quantity']} {order['ticker']} @ ${order.get('price', 'market')} via {len(order['selected_brokers'])} brokers")

            broker_status = {"successful": [], "failed": [], "skipped": []}
            broker_tasks = {}

            # Create concurrent async tasks for this order
            for broker in order["selected_brokers"]:
                trade_function = trade_functions.get(broker)
                if not trade_function:
                    add_response_fn(f"⚠️ {broker}: No trade function found")
                    broker_status["failed"].append(f"{broker} (no function)")
                else:
                    # Execute broker functions as native async tasks (all broker functions are async)
                    broker_tasks[broker] = asyncio.create_task(
                        trade_function(
                            order["action"],
                            order["quantity"],
                            order["ticker"],
                            order["price"],
                        )
                    )

            # Execute broker tasks concurrently
            if broker_tasks:
                results = await asyncio.gather(*broker_tasks.values(), return_exceptions=True)

                for (broker, _), result in zip(broker_tasks.items(), results):
                    if isinstance(result, Exception):
                        # Exception raised during execution
                        broker_status["failed"].append(broker)
                        error_msg = str(result).split('\n')[0]
                        add_response_fn(f"   ❌ {broker}: {error_msg}")
                    elif result is True:
                        # Explicit success
                        broker_status["successful"].append(broker)
                        add_response_fn(f"   ✅ {broker}: Success")
                    elif result is False:
                        # Explicit failure (API error, insufficient funds, etc.)
                        broker_status["failed"].append(broker)
                        add_response_fn(f"   ❌ {broker}: Failed")
                    elif result is None:
                        # Broker skipped (no credentials or disabled)
                        broker_status["skipped"].append(broker)
                        add_response_fn(f"   ⚠️  {broker}: Skipped (no credentials)")
                    else:
                        # Unknown return value (shouldn't happen with proper broker implementation)
                        broker_status["failed"].append(broker)
                        add_response_fn(f"   ❌ {broker}: Unknown result ({result})")

            # Count broker-level statistics (not order-level)
            batch_successful_brokers += len(broker_status["successful"])
            batch_failed_brokers += len(broker_status["failed"])
            batch_skipped_brokers += len(broker_status["skipped"])

            batch_statuses.append(broker_status)

            # Show per-order summary
            order_summary_parts = []
            if broker_status["successful"]:
                order_summary_parts.append(f"✅ {len(broker_status['successful'])} succeeded")
            if broker_status["failed"]:
                order_summary_parts.append(f"❌ {len(broker_status['failed'])} failed")
            if broker_status["skipped"]:
                order_summary_parts.append(f"⚠️ {len(broker_status['skipped'])} skipped")

            if order_summary_parts:
                add_response_fn(f"   📊 Order {idx} results: {', '.join(order_summary_parts)}")

            if idx < total_orders:
                add_response_fn("", force_redraw=True)

        return {
            "successful": batch_successful_brokers,
            "failed": batch_failed_brokers,
            "skipped": batch_skipped_brokers,
            "statuses": batch_statuses
        }


# Global order batch processor
order_processor = OrderBatchProcessor()
