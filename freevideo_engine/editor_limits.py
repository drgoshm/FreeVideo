"""Shared byte limit for a complete saved editor project, including its receipt."""

# Keep the existing reader's allowance for the atomic save receipt.
PROJECT_BYTES = 8 * 1024 * 1024 + 256
