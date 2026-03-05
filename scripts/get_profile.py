"""View DID Profile (own or public).

Usage:
    # View own Profile
    uv run python scripts/get_profile.py

    # View public Profile of a specific DID
    uv run python scripts/get_profile.py --did "did:wba:localhost:user:abc123"

    # Resolve a DID document
    uv run python scripts/get_profile.py --resolve "did:wba:localhost:user:abc123"

[INPUT]: SDK (RPC calls), credential_store (load identity credentials)
[OUTPUT]: Profile information as JSON output
[POS]: Profile query script

[PROTOCOL]:
1. Update this header when logic changes
2. Check the folder's CLAUDE.md after updating
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from utils import SDKConfig, create_user_service_client, rpc_call, authenticated_rpc_call, resolve_to_did
from credential_store import create_authenticator


PROFILE_RPC = "/user-service/did/profile/rpc"


async def get_my_profile(credential_name: str = "default") -> None:
    """View own Profile."""
    config = SDKConfig()
    auth_result = create_authenticator(credential_name, config)
    if auth_result is None:
        print(f"Credential '{credential_name}' unavailable; please create an identity first")
        sys.exit(1)

    auth, _ = auth_result
    async with create_user_service_client(config) as client:
        me = await authenticated_rpc_call(
            client, PROFILE_RPC, "get_me",
            auth=auth, credential_name=credential_name,
        )
        print(json.dumps(me, indent=2, ensure_ascii=False))


async def get_public_profile(did: str) -> None:
    """View public Profile of a specific DID."""
    config = SDKConfig()
    async with create_user_service_client(config) as client:
        profile = await rpc_call(
            client, PROFILE_RPC, "get_public_profile", {"did": did}
        )
        print(json.dumps(profile, indent=2, ensure_ascii=False))


async def resolve_did(did: str) -> None:
    """Resolve a DID document."""
    config = SDKConfig()
    async with create_user_service_client(config) as client:
        resolved = await rpc_call(
            client, PROFILE_RPC, "resolve", {"did": did}
        )
        print(json.dumps(resolved, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="View DID Profile")
    parser.add_argument("--did", type=str, help="View public Profile of a specific DID or handle")
    parser.add_argument("--resolve", type=str, help="Resolve a specific DID document")
    parser.add_argument("--credential", type=str, default="default",
                        help="Credential name (default: default)")

    args = parser.parse_args()

    if args.resolve:
        resolved_did = asyncio.run(resolve_to_did(args.resolve))
        asyncio.run(resolve_did(resolved_did))
    elif args.did:
        target_did = asyncio.run(resolve_to_did(args.did))
        asyncio.run(get_public_profile(target_did))
    else:
        asyncio.run(get_my_profile(args.credential))


if __name__ == "__main__":
    main()
