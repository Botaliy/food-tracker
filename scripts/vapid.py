"""Print a new pair of Web Push (VAPID) keys for .env.

    python -m scripts.vapid

Generate once per server: new keys invalidate every device's subscription.
"""

from app.push import generate_keys


def main() -> None:
    private, public = generate_keys()
    print(f"VAPID_PRIVATE_KEY={private}")
    print(f"VAPID_PUBLIC_KEY={public}")


if __name__ == "__main__":
    main()
