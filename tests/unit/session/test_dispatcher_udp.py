import asyncio
from unittest.mock import AsyncMock

import pytest
from exectunnel.session._config import TunnelConfig
from exectunnel.session._dispatcher import RequestDispatcher, _ActiveUdpFlow


@pytest.mark.asyncio
async def test_reap_idle_udp_flows():
    # Setup
    tun_cfg = TunnelConfig(udp_flow_idle_timeout=0.1)  # 100ms
    dispatcher = RequestDispatcher(
        tun_cfg=tun_cfg,
        ws_send=AsyncMock(),
        ws_closed=asyncio.Event(),
        tcp_registry={},
        pending_connects={},
        udp_registry={},
        pre_ack_buffer_cap_bytes=1024,
    )

    mock_flow_active = AsyncMock()
    mock_flow_idle = AsyncMock()

    now = asyncio.get_running_loop().time()

    active_flows = {
        ("active.test", 1234): _ActiveUdpFlow(
            handler=mock_flow_active, src_ip="1.1.1.1", dst_port=1234, last_used_at=now
        ),
        ("idle.test", 5678): _ActiveUdpFlow(
            handler=mock_flow_idle,
            src_ip="2.2.2.2",
            dst_port=5678,
            last_used_at=now - 0.2,  # 200ms ago, should be reaped
        ),
    }

    # Execute
    await dispatcher._reap_idle_udp_flows(active_flows)

    # Verify
    assert ("active.test", 1234) in active_flows
    assert ("idle.test", 5678) not in active_flows
    mock_flow_idle.close.assert_called_once()
    mock_flow_active.close.assert_not_called()


@pytest.mark.asyncio
async def test_reap_idle_udp_flows_disabled():
    # Setup
    tun_cfg = TunnelConfig(udp_flow_idle_timeout=0)  # Disabled
    dispatcher = RequestDispatcher(
        tun_cfg=tun_cfg,
        ws_send=AsyncMock(),
        ws_closed=asyncio.Event(),
        tcp_registry={},
        pending_connects={},
        udp_registry={},
        pre_ack_buffer_cap_bytes=1024,
    )

    mock_flow_idle = AsyncMock()

    now = asyncio.get_running_loop().time()

    active_flows = {
        ("idle.test", 5678): _ActiveUdpFlow(
            handler=mock_flow_idle,
            src_ip="2.2.2.2",
            dst_port=5678,
            last_used_at=now - 1000,  # Way in the past
        )
    }

    # Execute
    await dispatcher._reap_idle_udp_flows(active_flows)

    # Verify
    assert ("idle.test", 5678) in active_flows
    mock_flow_idle.close.assert_not_called()
