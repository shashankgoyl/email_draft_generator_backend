"""
manage_users.py — User management functions.

Used by admin_router.py via import.
Also runnable as a CLI script locally:

    python manage_users.py add    <email> <password>
    python manage_users.py delete <email>
    python manage_users.py list
    python manage_users.py reset  <email> <new_password>
"""

import sys
import bcrypt
from dotenv import load_dotenv

load_dotenv()


def _hash(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def add_user(email: str, password: str) -> bool:
    """Add a new user. Returns True on success, False if already exists.
    Raises ValueError if password is shorter than 6 characters."""
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    from database import db
    return db.create_user(email, _hash(password))


def delete_user(email: str) -> bool:
    """Delete user by email. Returns True if deleted, False if not found."""
    from database import db
    return db.delete_user(email)


def list_users() -> list:
    """Return list of all users (id, email, created_at)."""
    from database import db
    return db.list_users()


def reset_password(email: str, new_password: str) -> bool:
    """Reset a user's password. Raises ValueError if password too short."""
    if len(new_password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    from database import db
    return db.reset_password(email, _hash(new_password))


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    cmd = args[0].lower()

    if cmd == "add" and len(args) == 3:
        try:
            ok = add_user(args[1], args[2])
            print("✅ User added:" if ok else "⚠️  Already exists:", args[1])
        except ValueError as e:
            print(f"❌ {e}"); sys.exit(1)

    elif cmd == "delete" and len(args) == 2:
        ok = delete_user(args[1])
        print("✅ Deleted:" if ok else "⚠️  Not found:", args[1])

    elif cmd == "list" and len(args) == 1:
        users = list_users()
        if not users:
            print("No users found.")
        else:
            print(f"\n{'ID':<5} {'Email':<40} {'Created At'}")
            print("-" * 75)
            for u in users:
                print(f"{u['id']:<5} {u['email']:<40} {u['created_at']}")

    elif cmd == "reset" and len(args) == 3:
        try:
            ok = reset_password(args[1], args[2])
            print("✅ Password reset:" if ok else "⚠️  Not found:", args[1])
        except ValueError as e:
            print(f"❌ {e}"); sys.exit(1)

    else:
        print(__doc__); sys.exit(1)
