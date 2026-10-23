"""Plugin-owned SDK KV migration; never edits the host or a game database."""

from plugin.sdk.plugin import Err, unwrap_or


async def migrate_legacy_store(source, target):
    """Copy only missing keys; never overwrite an existing external history."""
    source.enabled = target.enabled = True
    dumped = await source.dump()
    if isinstance(dumped, Err):
        raise RuntimeError("HSR_LEGACY_STORE_READ_FAILED")
    copied = 0
    for key, value in (unwrap_or(dumped, {}) or {}).items():
        exists = await target.exists(key)
        if isinstance(exists, Err):
            raise RuntimeError("HSR_EXTERNAL_STORE_READ_FAILED")
        if unwrap_or(exists, False):
            continue
        result = await target.set(key, value)
        if isinstance(result, Err):
            raise RuntimeError("HSR_EXTERNAL_STORE_MIGRATION_FAILED")
        copied += 1
    return copied
