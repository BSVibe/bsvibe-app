"""Static guards for the sandbox network-isolation + Redis-auth deploy config.

These assertions cannot exercise nftables-in-DinD (CI has no privileged Docker),
so they instead pin the *static* invariants of the deploy artifacts: the exact
firewall rules the DinD wrapper installs, that it wraps (not replaces) the stock
dind entrypoint, that it never touches the backend->daemon path, and that Redis
runs with a password wired through a single source. Live enforcement is proven
by docs/e2e/sandbox-network-isolation-checklist.md against the real Mac-Mini.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "deploy"

# RFC1918 + link-local. STATIC — these must all be blocked from nested sandboxes.
_PRIVATE_RANGES = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
)
_NESTED_BRIDGE = "docker0"


@pytest.fixture(scope="module")
def firewall() -> str:
    return (_DEPLOY / "sandbox-dind-firewall.sh").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_base() -> str:
    return (_DEPLOY / "compose.yaml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_prod() -> str:
    return (_DEPLOY / "compose.prod.yaml").read_text(encoding="utf-8")


class TestFirewallScript:
    def test_script_exists_and_is_executable(self) -> None:
        path = _DEPLOY / "sandbox-dind-firewall.sh"
        assert path.exists(), "sandbox-dind-firewall.sh is missing"
        # Owner-executable bit — the Dockerfile also chmods, but keep the source honest.
        assert path.stat().st_mode & 0o100, "firewall script is not executable"

    def test_nested_bridge_is_pinned_to_docker0(self, firewall: str) -> None:
        # The rules are scoped by this variable; pin its value.
        assert f'NESTED_BRIDGE="{_NESTED_BRIDGE}"' in firewall

    @pytest.mark.parametrize("cidr", _PRIVATE_RANGES)
    def test_each_private_range_present(self, firewall: str, cidr: str) -> None:
        # Every private range must appear in the STATIC PRIVATE_RANGES list.
        assert cidr in firewall, f"private range {cidr} not blocked"

    def test_private_ranges_are_static_list(self, firewall: str) -> None:
        joined = " ".join(_PRIVATE_RANGES)
        assert joined in firewall, "PRIVATE_RANGES drifted from the expected static set"

    def test_forward_drop_scoped_to_nested_bridge(self, firewall: str) -> None:
        # The FORWARD drops must be scoped to traffic ORIGINATING on the nested
        # bridge (in-interface), never global — else public egress breaks.
        assert 'DOCKER-USER -i "$NESTED_BRIDGE" -d "$cidr" -j DROP' in firewall

    def test_daemon_port_2375_blocked_on_nested_bridge_input(self, firewall: str) -> None:
        # Nested -> gateway:2375 arrives as INPUT (destined to the daemon itself),
        # not FORWARD, so it needs an explicit INPUT drop scoped to the bridge.
        assert 'DAEMON_PORT="2375"' in firewall
        assert 'INPUT -i "$NESTED_BRIDGE" -p tcp --dport "$DAEMON_PORT" -j DROP' in firewall

    def test_uses_docker_user_chain_for_forward_rules(self, firewall: str) -> None:
        # DOCKER-USER is the only FORWARD-chain slot Docker never flushes.
        assert "DOCKER-USER" in firewall

    def test_wraps_not_replaces_stock_dind_entrypoint(self, firewall: str) -> None:
        # The wrapper MUST exec the stock dind entrypoint (which does TLS/iptables
        # scaffolding + storage setup). Replacing it silently breaks the daemon.
        assert "exec dockerd-entrypoint.sh" in firewall

    def test_no_drop_rule_on_external_interface(self, firewall: str) -> None:
        # The backend->daemon control path arrives on eth0. The firewall must
        # NEVER install a rule on eth0 (that would break sandbox management).
        assert "-i eth0" not in firewall


class TestDindImageIsHardened:
    def test_compose_builds_hardened_dind_image(self, compose_base: str) -> None:
        # sandbox-dind must build the wrapper image, not run stock docker:28-dind.
        assert "Dockerfile.sandbox-dind" in compose_base
        dockerfile = _DEPLOY / "Dockerfile.sandbox-dind"
        assert dockerfile.exists()
        text = dockerfile.read_text(encoding="utf-8")
        assert "FROM docker:28-dind" in text
        assert "sandbox-dind-firewall.sh" in text
        assert "ENTRYPOINT" in text

    def test_sandbox_dind_is_isolated_on_sandboxnet(self, compose_base: str) -> None:
        # :2375 hardening (part 2): sandbox-dind is reachable only from
        # backend/worker via a dedicated network, not the whole app network.
        assert "sandboxnet" in compose_base


class TestRedisAuth:
    def test_prod_redis_requires_password(self, compose_prod: str) -> None:
        assert "--requirepass" in compose_prod
        assert "BSVIBE_REDIS_PASSWORD" in compose_prod

    def test_prod_healthcheck_authenticates(self, compose_prod: str) -> None:
        # A password'd Redis makes the base `redis-cli ping` return NOAUTH; the
        # prod override must authenticate or the container never goes healthy.
        assert "redis-cli -a" in compose_prod

    def test_redis_url_carries_password_single_source(self, compose_prod: str) -> None:
        # The app's URL default embeds the same BSVIBE_REDIS_PASSWORD — one source
        # of truth, no drift between requirepass and the client credential.
        assert "redis://:${BSVIBE_REDIS_PASSWORD}@redis:6379/0" in compose_prod
