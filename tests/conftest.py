import os

# The API refuses to sign tokens without a secret; tests don't need the real one.
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789")
