"""Order processing and execution for the TUI."""

import asyncio
from .config import broker_thread_pool, thread_pool_stats


def run_broker_trade(trade_func, *args):
    """
    Execute a broker trade function efficiently.
    Handles both sync and async broker functions.
    """
    global thread_pool_stats
    thread_pool_stats["tasks_submitted"] += 1

    try:
        if asyncio.iscoroutinefunction(trade_func):
            # For async functions, run in new event loop
            result = asyncio.run(trade_func(*args))
        else:
            # For sync functions, call directly
            result = trade_func(*args)

        thread_pool_stats["tasks_completed"] += 1
        return result
    except Exception as e:
        thread_pool_stats["tasks_failed"] += 1
        # Ensure exceptions are properly propagated
        raise e


def cleanup_thread_pool():
    """Properly shutdown the broker thread pool."""
    global broker_thread_pool
    if broker_thread_pool:
        try:
            # Wait for current tasks to complete (with timeout)
            broker_thread_pool.shutdown(wait=True, timeout=5.0)
        except Exception:
            # Force shutdown if timeout exceeded
            broker_thread_pool.shutdown(wait=False)
        finally:
            broker_thread_pool = None


class OrderBatchProcessor:
    """Process orders in batches for better performance."""

    def __init__(self, batch_size=5):
        self.batch_size = batch_size

    async def process_orders(self, orders, trade_functions, add_response_fn):
        """Process orders in batches."""
        total_orders = len(orders)
        successful_orders = 0
        failed_orders = 0
        all_broker_statuses = []

        for i in range(0, total_orders, self.batch_size):
            batch = orders[i:i + self.batch_size]
            batch_results = await self._process_batch(batch, trade_functions, add_response_fn, i + 1, total_orders)

            successful_orders += batch_results["successful"]
            failed_orders += batch_results["failed"]
            all_broker_statuses.extend(batch_results["statuses"])

        return {
            "successful": successful_orders,
            "failed": failed_orders,
            "statuses": all_broker_statuses
        }

    async def _process_batch(self, batch, trade_functions, add_response_fn, start_idx, total_orders):
        """Process a batch of orders."""
        batch_successful = 0
        batch_failed = 0
        batch_statuses = []

        for idx, order in enumerate(batch, start_idx):
            progress = f"[{idx}/{total_orders}]"
            add_response_fn(f"{progress} {order['action'].upper()} {order['quantity']} {order['ticker']} @ ${order.get('price', 'market')} via {len(order['selected_brokers'])} brokers")

            broker_status = {"successful": [], "failed": []}
            broker_tasks = {}

            # Create concurrent tasks for this order
            for broker in order["selected_brokers"]:
                trade_function = trade_functions.get(broker)
                if not trade_function:
                    add_response_fn(f"⚠️ {broker}: No trade function found")
                    broker_status["failed"].append(f"{broker} (no function)")
                else:
                    # Use thread pool for efficient execution
                    broker_tasks[broker] = asyncio.get_event_loop().run_in_executor(
                        broker_thread_pool,
                        run_broker_trade,
                        trade_function,
                        order["action"],
                        order["quantity"],
                        order["ticker"],
                        order["price"],
                    )

            # Execute broker tasks concurrently
            if broker_tasks:
                results = await asyncio.gather(*broker_tasks.values(), return_exceptions=True)

                for (broker, _), result in zip(broker_tasks.items(), results):
                    if isinstance(result, Exception):
                        broker_status["failed"].append(broker)
                        error_msg = str(result).split('\n')[0]
                        add_response_fn(f"   ❌ {broker}: {error_msg}")
                    else:
                        broker_status["successful"].append(broker)
                        add_response_fn(f"   ✅ {broker}: Success")

            # Update batch statistics
            if broker_status["successful"]:
                add_response_fn(f"   📈 Successful brokers: {', '.join(broker_status['successful'])}")
                batch_successful += 1
            else:
                add_response_fn(f"   📉 Failed brokers: {', '.join(broker_status['failed'])}")
                batch_failed += 1

            batch_statuses.append(broker_status)

            if idx < total_orders:
                add_response_fn("", force_redraw=True)

        return {
            "successful": batch_successful,
            "failed": batch_failed,
            "statuses": batch_statuses
        }


# Global order batch processor
order_processor = OrderBatchProcessor()
