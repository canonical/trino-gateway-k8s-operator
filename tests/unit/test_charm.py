# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
#
# To learn more about testing, see https://canonical.com/juju/docs/ops/latest/explanation/testing/

from ops import testing

from charm import TrinoGatewayK8SOperatorCharm


def test_pebble_ready_sets_active_status():
    """Test that the charm has the correct state after handling the pebble-ready event."""
    ctx = testing.Context(TrinoGatewayK8SOperatorCharm)
    container = testing.Container("trino-gateway", can_connect=True)
    state_out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))

    assert state_out.unit_status == testing.ActiveStatus()
