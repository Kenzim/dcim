# Security Review and Improvements

## Summary

This document outlines security vulnerabilities found and fixes implemented in the DCIM application.

## Security Issues Found and Fixed

### 1. Unauthenticated File Serving Endpoints ✅ FIXED

**Issue:** Multiple endpoints served files without authentication, allowing unauthorized access to:
- Installation files (install.wim, ISOs)
- Scripts
- Disk images
- OS templates

**Affected Endpoints:**
- `/api/servers/interaction/disk-images/{filename}` - Served install.wim and other disk images
- `/api/servers/interaction/isos/{filename}` - Served ISO files
- `/api/servers/interaction/scripts/{task_id}` - Served boot task scripts
- `/api/servers/interaction/scripts/by-id/{script_id_or_name}` - Served script definitions

**Fix:** Implemented one-time download token system:
- Tokens are generated when boot tasks are created
- Tokens are injected into installation scripts as `${DOWNLOAD_TOKEN}`
- Tokens are validated on file-serving endpoints
- Tokens are single-use (marked as used after first access)
- Tokens expire after 24 hours

### 2. Unauthenticated Script Access ✅ FIXED

**Issue:** Script definitions could be accessed without authentication via `/api/servers/interaction/scripts/by-id/{script_id_or_name}`

**Fix:** Added authentication requirement - either admin token or valid download token

### 3. Unauthenticated Log Upload ✅ FIXED

**Issue:** Installation log upload endpoint (`/api/installation-tasks/{task_id}/logs`) had no authentication, allowing anyone to update logs

**Fix:** Added optional token validation (backward compatible, but recommended)

## Implementation Details

### One-Time Token Service

Created `app/services/download_token_service.py` with the following features:

- **Token Generation:** Generates secure random tokens (32 bytes, URL-safe)
- **Token Storage:** Stored in Redis with metadata (boot_task_id, expiration, allowed files)
- **Token Validation:** Validates token existence, expiration, and file access permissions
- **Single-Use:** Tokens are marked as used after first access
- **File Restrictions:** Tokens can be restricted to specific files or patterns

### Token Injection

Tokens are automatically:
1. Generated when boot tasks are created (for template-based installations)
2. Injected into installation scripts as `${DOWNLOAD_TOKEN}` variable
3. Added to file download URLs (e.g., `?token=...`)

### Updated Endpoints

All file-serving endpoints now require token validation:
- `/api/servers/interaction/disk-images/{filename}` - **REQUIRES TOKEN**
- `/api/servers/interaction/isos/{filename}` - **REQUIRES TOKEN**
- `/api/servers/interaction/scripts/{task_id}` - **OPTIONAL TOKEN** (backward compatible)
- `/api/servers/interaction/scripts/by-id/{script_id_or_name}` - **REQUIRES AUTH OR TOKEN**

## Remaining Considerations

### Endpoints That Remain Open (By Design)

Some endpoints intentionally remain open for PXE boot functionality:

- `/api/servers/interaction/pxe` - Validates MAC address (sufficient for PXE)
- `/api/servers/interaction/kernel/{filename}` - Needed for initial PXE boot
- `/api/servers/interaction/initrd/{filename}` - Needed for initial PXE boot
- `/api/servers/interaction/temp-os/{os_id}/files/{filename}` - Could be secured in future
- `/api/servers/interaction/images/{filename}` - Could be secured in future

**Note:** These endpoints should be protected by network-level security (firewall rules, VLAN isolation) since they're needed for PXE booting before authentication is possible.

### Recommendations

1. **Network Security:** Ensure PXE endpoints are only accessible from trusted networks
2. **Rate Limiting:** Consider adding rate limiting to file-serving endpoints
3. **Token Rotation:** Consider implementing token rotation for long-running installations
4. **Audit Logging:** Add audit logging for token usage and file access
5. **HTTPS:** Ensure all endpoints are served over HTTPS in production

## Testing

To test the security improvements:

1. **Test Token Generation:**
   - Create a boot task for a template installation
   - Verify token is generated and injected into script

2. **Test Token Validation:**
   - Try accessing `/api/servers/interaction/disk-images/install.wim` without token → Should fail
   - Try accessing with invalid token → Should fail
   - Try accessing with valid token → Should succeed
   - Try accessing same token again → Should fail (single-use)

3. **Test Script Injection:**
   - Check installation script contains `${DOWNLOAD_TOKEN}` variable
   - Verify URLs include token parameter

## Files Modified

- `app/services/download_token_service.py` - **NEW** - Token service implementation
- `app/api/server_interaction.py` - Updated boot task creation and file-serving endpoints
- `app/api/billing.py` - Updated boot task creation for billing API
- `app/api/installation_tasks.py` - Added token validation to log upload endpoint

## Migration Notes

- Existing boot tasks created before this update will not have tokens
- Scripts using old URLs without tokens will fail (update scripts to use `${DOWNLOAD_TOKEN}` variable)
- For backward compatibility, `/api/servers/interaction/scripts/{task_id}` accepts requests without tokens (but logs a warning)
