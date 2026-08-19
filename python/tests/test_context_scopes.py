import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import metergraph
from metergraph import _context


def test_context_nests_merges_and_restores_after_exception():
    metergraph.set_default_tags(service="support")

    try:
        with metergraph.context(session_id="outer", tags={"customer": "acme"}):
            assert _context.snapshot().session_id == "outer"
            assert _context.snapshot().tags == {
                "service": "support",
                "customer": "acme",
            }

            try:
                with metergraph.session("inner"):
                    with metergraph.tags(customer="globex", region="us"):
                        assert _context.snapshot().session_id == "inner"
                        assert _context.snapshot().tags == {
                            "service": "support",
                            "customer": "globex",
                            "region": "us",
                        }
                        raise RuntimeError("boom")
            except RuntimeError:
                pass

            assert _context.snapshot().session_id == "outer"
            assert _context.snapshot().tags == {
                "service": "support",
                "customer": "acme",
            }

        assert _context.snapshot().session_id is None
        assert _context.snapshot().tags == {"service": "support"}
    finally:
        metergraph.set_default_tags()


def test_overlapping_async_jobs_do_not_leak_context():
    async def job(session_id: str):
        with metergraph.context(session_id=session_id, tags={"job": session_id}):
            await asyncio.sleep(0)
            return _context.snapshot()

    async def run():
        return await asyncio.gather(job("one"), job("two"))

    one, two = asyncio.run(run())
    assert (one.session_id, one.tags) == ("one", {"job": "one"})
    assert (two.session_id, two.tags) == ("two", {"job": "two"})
    assert _context.snapshot().session_id is None
    assert _context.snapshot().tags == {}


def test_context_decorator_and_wrapped_executor_propagate_scope():
    @metergraph.context(session_id="decorated", tags={"source": "decorator"})
    async def decorated():
        with ThreadPoolExecutor(max_workers=1) as raw:
            executor = metergraph.wrap_executor(raw)
            return await asyncio.get_running_loop().run_in_executor(
                executor, _context.snapshot
            )

    observed = asyncio.run(decorated())
    assert observed.session_id == "decorated"
    assert observed.tags == {"source": "decorator"}


def test_legacy_setters_only_update_an_active_scope(caplog):
    with caplog.at_level(logging.WARNING, logger="metergraph"):
        metergraph.set_session("ignored")
        metergraph.set_tags(ignored=True)

    assert _context.snapshot().session_id is None
    assert _context.snapshot().tags == {}
    assert "active Metergraph context" in caplog.text

    with metergraph.route("active"):
        metergraph.set_session("accepted")
        metergraph.set_tags(tier="pro")
        assert _context.snapshot().session_id == "accepted"
        assert _context.snapshot().tags == {"tier": "pro"}

    assert _context.snapshot().session_id is None
    assert _context.snapshot().tags == {}
