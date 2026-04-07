"""Unregister (revoke) a Handle and delete local credentials.

Usage:
    # Step 1: send revoke verification code to all bound channels
    uv run python scripts/unregister_handle.py --handle alice

    # Step 2: confirm revoke with the received code
    uv run python scripts/unregister_handle.py --handle alice --code 123456

[INPUT]: SDKConfig, credential_store (load/delete identity), utils.handle (request/confirm revoke)
[OUTPUT]: Handle revoke request/confirmation and local credential cleanup
[POS]: Pure non-interactive CLI for Handle revocation. Requires explicit --handle
       and uses the current default credential for authentication.

[PROTOCOL]:
1. Update this header when logic changes
2. Check the folder's CLAUDE.md after updating
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from credential_store import (
    create_authenticator,
    delete_identity,
    list_identities,
    load_identity,
)
from utils import SDKConfig, create_user_service_client
from utils.cli_errors import exit_with_cli_error
from utils.handle import confirm_handle_revoke, request_handle_revoke
from utils.logging_config import configure_logging
from utils.rpc import JsonRpcError

logger = logging.getLogger(__name__)


def _resolve_credential_for_handle(
    handle: str,
    *,
    requested_credential: str | None = None,
) -> str:
    """Resolve which credential should be used for a given handle.

    Priority:
    1) If --credential is provided, always use it (and enforce handle match later).
    2) Otherwise, scan all identities and find those whose index-entry handle matches.
       - If exactly one match, use that credential name.
       - If none, ask the caller to specify --credential explicitly.
       - If multiple, require explicit --credential to disambiguate.
    """
    if requested_credential:
        return requested_credential

    normalized = handle.strip()
    matches: list[str] = []
    for entry in list_identities():
        if str(entry.get("handle") or "") == normalized:
            matches.append(str(entry.get("credential_name")))

    if not matches:
        raise ValueError(
            f"No credential found for handle '{handle}'. "
            "Please specify --credential explicitly or run "
            "check_status.py --list to inspect saved identities."
        )
    if len(matches) > 1:
        raise ValueError(
            "Multiple credentials found for handle "
            f"'{handle}': {matches}. Please specify --credential explicitly."
        )
    return matches[0]


async def _do_request_revoke(handle: str, credential_name: str | None) -> None:
    """Send revoke verification code for a given handle.

    The backend sends the same numeric revoke code to all bound channels
    (phone and email) for this handle.
    """
    config = SDKConfig.load()
    logger.info("Requesting handle revoke handle=%s credential=%s", handle, credential_name or "<auto>")

    resolved_credential = _resolve_credential_for_handle(
        handle,
        requested_credential=credential_name,
    )
    identity = load_identity(resolved_credential)
    if identity is None:
        raise ValueError(
            f"Credential '{resolved_credential}' not found. "
            "Please create an identity first."
        )
    if identity.get("handle") != handle:
        raise ValueError(
            f"Credential '{resolved_credential}' handle '{identity.get('handle')}' does not match "
            f"requested handle '{handle}'. Please switch credentials or adjust the handle."
        )

    auth_result = create_authenticator(resolved_credential, config)
    if auth_result is None:
        raise ValueError(
            f"Credential '{resolved_credential}' is missing auth files; "
            "please recreate the identity before revoking the handle."
        )
    auth, _ = auth_result

    async with create_user_service_client(config) as client:
        result = await request_handle_revoke(
            client,
            handle=handle,
            auth=auth,
            credential_name=resolved_credential,
        )

    sent = result.get("sent", 0)
    skipped = result.get("skipped", 0)
    print(
        f"Revoke code sent for handle {handle} to all bound channels "
        f"(phone/email): sent={sent}, skipped={skipped}"
    )
    print(
        "The same revoke code is sent to every bound channel. "
        "Check your bound phone/email and then rerun this command with "
        "--code <received-code> to confirm."
    )


async def _do_confirm_revoke(handle: str, code: str, credential_name: str | None) -> None:
    """Confirm handle revoke and delete local credential."""
    config = SDKConfig.load()
    logger.info("Confirming handle revoke handle=%s credential=%s", handle, credential_name or "<auto>")

    resolved_credential = _resolve_credential_for_handle(
        handle,
        requested_credential=credential_name,
    )
    identity = load_identity(resolved_credential)
    if identity is None:
        raise ValueError(
            f"Credential '{resolved_credential}' not found. "
            "Please create an identity first."
        )
    if identity.get("handle") != handle:
        raise ValueError(
            f"Credential '{resolved_credential}' handle '{identity.get('handle')}' does not match "
            f"requested handle '{handle}'. Please switch credentials or adjust the handle."
        )

    auth_result = create_authenticator(resolved_credential, config)
    if auth_result is None:
        raise ValueError(
            f"Credential '{resolved_credential}' is missing auth files; "
            "please recreate the identity before revoking the handle."
        )
    auth, _ = auth_result

    already_revoked = False
    async with create_user_service_client(config) as client:
        try:
            result = await confirm_handle_revoke(
                client,
                handle=handle,
                code=code,
                auth=auth,
                credential_name=resolved_credential,
            )
        except JsonRpcError as exc:
            # When the DID has already been revoked or removed on the server side,
            # DIDWba authentication may fail with a "DID not found or revoked" style
            # error. In that case we treat the handle as already revoked remotely
            # and proceed with local credential cleanup to keep things idempotent.
            message = getattr(exc, "message", str(exc))
            if "DID not found or revoked" in message or "DIDWba authentication failed" in message:
                already_revoked = True
                logger.warning(
                    "Handle revoke confirm reported missing/revoked DID; "
                    "treating as already revoked: %s",
                    message,
                )
                result = {"ok": False, "already_revoked": True, "error": message}
            else:
                raise

    if not result.get("ok") and not result.get("already_revoked"):
        raise RuntimeError(f"Handle revoke failed for {handle}: {result}")

    # Delete local credential for the resolved name
    deleted = delete_identity(resolved_credential)
    if already_revoked:
        print(f"Handle '{handle}' was already revoked on server side. Local cleanup completed.")
    else:
        print(f"Handle '{handle}' has been revoked on server side.")
    if deleted:
        print(f"Local credential '{resolved_credential}' has been deleted.")
    else:
        print(f"Local credential '{resolved_credential}' not found or could not be deleted.")


def main() -> None:
    """CLI entry point."""
    configure_logging(console_level=None, mirror_stdio=True)

    parser = argparse.ArgumentParser(
        description="Unregister (revoke) a Handle and delete local credentials",
    )
    parser.add_argument(
        "--handle",
        required=True,
        type=str,
        help="Handle local-part to revoke (e.g., alice)",
    )
    parser.add_argument(
        "--code",
        type=str,
        default=None,
        help=(
            "Verification code received via SMS/email. The same code is "
            "sent to all bound channels. If omitted, only sends the "
            "revoke code."
        ),
    )
    parser.add_argument(
        "--credential",
        type=str,
        default=None,
        help=(
            "Credential name to use for authentication. "
            "Defaults to the one bound to --handle; if multiple credentials "
            "share the same handle, this flag becomes required."
        ),
    )

    args = parser.parse_args()
    try:
        if args.code:
            asyncio.run(_do_confirm_revoke(args.handle, args.code, args.credential))
        else:
            asyncio.run(_do_request_revoke(args.handle, args.credential))
    except ValueError as exc:
        exit_with_cli_error(
            exc=exc,
            logger=logger,
            context="unregister_handle CLI validation failed",
            exit_code=2,
            log_traceback=False,
        )
    except Exception as exc:  # noqa: BLE001
        exit_with_cli_error(
            exc=exc,
            logger=logger,
            context="unregister_handle CLI failed",
        )


if __name__ == "__main__":
    main()
