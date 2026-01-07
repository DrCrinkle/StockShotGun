"""Holdings view for displaying broker account positions."""


class HoldingsView:
    """View for navigating and displaying holdings across multiple accounts."""

    def __init__(self, holdings_data, broker_name):
        self.holdings_data = holdings_data
        self.broker_name = broker_name
        self.account_ids = list(holdings_data.keys())
        self.current_index = 0

    def get_current_account(self):
        """Get the currently selected account ID."""
        return self.account_ids[self.current_index]

    def next_account(self):
        """Move to the next account."""
        self.current_index = (self.current_index + 1) % len(self.account_ids)

    def prev_account(self):
        """Move to the previous account."""
        self.current_index = (self.current_index - 1) % len(self.account_ids)

    def get_current_holdings_text(self):
        """Get formatted text for the current account's holdings."""
        account_id = self.get_current_account()
        positions = self.holdings_data[account_id]

        text = f"{self.broker_name} Holdings - Account {account_id} ({self.current_index + 1}/{len(self.account_ids)}):\n\n"

        if not positions:
            text += "No positions\n"
        else:
            for position in positions:
                # Handle None values for cost_basis and current_value
                cost_basis = position.get('cost_basis')
                if cost_basis is None:
                    cost_basis_display = "N/A"
                else:
                    cost_basis_display = f"${float(cost_basis):.2f}"

                current_value = position.get('current_value')
                if current_value is None:
                    current_value_display = "N/A"
                else:
                    current_value_display = f"${float(current_value):.2f}"

                text += (
                    f"{position['symbol']}: {position['quantity']} shares\n"
                    f"  Cost Basis: {cost_basis_display}\n"
                    f"  Current Value: {current_value_display}\n\n"
                )
        return text
