import os
from dotenv import load_dotenv

load_dotenv("./.env")

def validate_credentials(service, credentials):
    """Validate that required credentials are provided."""
    missing = []
    for env_var, prompt in credentials:
        value = os.getenv(env_var) or os.getenv(f"SSG_{env_var}")
        if not value:
            missing.append(prompt)
    
    if missing:
        print(f"⚠️  Warning: Missing {service} credentials: {', '.join(missing)}")
        return False
    return True

def setup():
    print("Setting up broker credentials, press ENTER to skip entry")

    brokers = {
        "Robinhood": [
            ("ROBINHOOD_USER", "Username"),
            ("ROBINHOOD_PASS", "Password"),
            ("ROBINHOOD_MFA", "MFA"),
        ],
        "Firstrade": [
            ("FIRSTRADE_USER", "Username"),
            ("FIRSTRADE_PASS", "Password"),
            ("FIRSTRADE_MFA", "MFA Secret")
        ],
        "Schwab": [
            ("SCHWAB_API_KEY", "API Key"),
            ("SCHWAB_API_SECRET", "API Secret"),
            ("SCHWAB_CALLBACK_URL", "Callback URL"),
            ("SCHWAB_TOKEN_PATH", "Token Path"),
        ],
        "Webull": [
            ("WEBULL_USER", "Username"),
            ("WEBULL_PASS", "Password"),
            ("WEBULL_MFA", "MFA TOTP Secret (if applicable)")
        ],
        "BBAE": [
            ("BBAE_USER", "Username"),
            ("BBAE_PASS", "Password"),
        ],
        "DSPAC": [
            ("DSPAC_USER", "Username"),
            ("DSPAC_PASS", "Password"),
        ],
        "Chase": [
            ("CHASE_USER", "Username"),
            ("CHASE_PASS", "Password"),
            ("CELL_PHONE_LAST_FOUR", "Last four digits of cell phone number"),
        ],
        "SoFi": [
            ("SOFI_USER", "Username"),
            ("SOFI_PASS", "Password"),
            ("SOFI_TOTP", "TOTP Secret (optional, press ENTER to skip)")
        ],
        "TastyTrade": [("TASTY_USER", "Username"), ("TASTY_PASS", "Password")],
        "Tradier": [("TRADIER_ACCESS_TOKEN", "Access Token")],
        "Public": [("PUBLIC_API_SECRET", "API Secret Key")],
        "Fennel": [("FENNEL_ACCESS_TOKEN", "Personal Access Token (from Fennel Dashboard)")],
    }

    # Check existing credentials first
    print("Checking existing credentials...")
    existing_services = []
    for service, credentials in brokers.items():
        if validate_credentials(service, credentials):
            existing_services.append(service)
            print(f"✓ {service}: Credentials found")
        else:
            print(f"✗ {service}: Credentials missing")
    
    if existing_services:
        print(f"\nExisting credentials found for: {', '.join(existing_services)}")
        skip_existing = input("Skip setup for existing services? (y/N): ").lower().startswith('y')
    else:
        skip_existing = False

    for service, credentials in brokers.items():
        # Skip if credentials exist and user chose to skip
        if skip_existing and validate_credentials(service, credentials):
            print(f"Skipping {service} (credentials already exist)")
            continue
            
        print(f"{'-' * 10}{service}{'-' * 10}")
        for env_var, prompt in credentials:
            # Check for existing value first
            existing_value = os.getenv(env_var) or os.getenv(f"SSG_{env_var}")
            if existing_value:
                print(f"{service} {prompt}: [existing value hidden] (press ENTER to keep)")
                value = input(f"New {service} {prompt} (or ENTER to keep existing): ") or existing_value
            else:
                value = input(f"{service} {prompt}: ") or ""
            
            # Store directly without SSG_ prefix to avoid duplication
            if value:
                os.environ[env_var] = value

    print(f"{'-' * 5} Saving credentials to .env {'-' * 5}")
    
    # Save credentials directly without SSG_ prefix
    with open(".env", 'w') as f:
        for service, credentials in brokers.items():
            for env_var, _ in credentials:
                value = os.getenv(env_var)
                if value:
                    f.write(f'{env_var}={value}\n')

    print("Credentials saved to .env")
    
    # Validate final configuration
    print("\nValidating final configuration...")
    final_validation = []
    for service, credentials in brokers.items():
        if validate_credentials(service, credentials):
            final_validation.append(service)
    
    if final_validation:
        print(f"✅ Configuration complete! Services ready: {', '.join(final_validation)}")
    else:
        print("⚠️  No services are fully configured. Please check your .env file.")
