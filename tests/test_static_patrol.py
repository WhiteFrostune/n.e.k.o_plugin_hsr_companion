"""D is scheduling only: stable UI cannot grant or renew answer facts."""
import asyncio
import copy
import time
from types import SimpleNamespace

from plugin.plugins.hsr_companion.tests.test_current_character_regions import raw_character
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_query_reuse import picture
from plugin.plugins.hsr_companion.tests.test_resident_observer import configure_loop, sample


def test_stable_detail_does_not_repeat_full_without_a_request(tmp_path, monkeypatch):
    import plugin.plugins.hsr_companion as root
    owner, values, _ = plugin_fixture(tmp_path)
    owner._target_character = lambda: ""
    values["observer_settings"] = {"enabled": True, "proactive_enabled": False}
    now = [100.]
    native_calls = []
    sequence = [0]
    data = picture()

    def capture(since):
        sequence[0] += 1
        return sample(data, seq=sequence[0])

    def read(payload, **kwargs):
        native_calls.append(now[0])
        value = raw_character()
        value["recognition"].pop("region_query", None)  # This stub represents a full producer.
        value["recognition"]["source_size"] = [640, 400]
        return copy.deepcopy(value)

    owner._vision = SimpleNamespace(analyze_live=read)
    configure_loop(owner, SimpleNamespace(health=lambda: {"connected": True,
        "frame_seq": sequence[0]+1, "game": {"running": True, "pid": 1, "minimized": False},
        "capture": {"state": "capturing", "isolated_window": True, "frame_semantics": "every_capture"}},
        fetch_frame=capture))
    monkeypatch.setattr(root, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))

    class Loop:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            now[0] += delay
            if now[0] >= 113:
                raise asyncio.CancelledError()
            await asyncio.sleep(0)

    monkeypatch.setattr(root, "asyncio", Loop())

    async def run():
        task = owner._observer_task = asyncio.create_task(owner._observer_loop())
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert 1 <= len(native_calls) <= 2, (native_calls, getattr(owner, "_patrol_status", {}))
    assert sequence[0] > len(native_calls)


def static_owner(tmp_path):
    from io import BytesIO

    from PIL import Image
    from plugin.plugins.hsr_companion.runtime.query_reuse import region_stamp
    owner, _, _ = plugin_fixture(tmp_path)
    raw = raw_character()
    raw["recognition"].pop("region_query", None)
    raw["recognition"]["source_size"] = [640, 400]
    raw["observation_serial"] = 4
    owner._committed_observation = raw
    owner._last_full_commit_serial = 4
    owner._analysis_game_pid = 1
    owner._observer_last_signature = b"source"
    stream = BytesIO()
    Image.new("RGB", (640, 400), "black").save(stream, format="BMP")
    data = stream.getvalue()
    controls = raw["recognition"]["scene_regions"]
    owner._accepted_frame_delivery_guard = (controls, region_stamp(data, controls))
    return owner, data


def altered(data, bounds):
    from io import BytesIO

    from PIL import Image, ImageDraw
    image = Image.open(BytesIO(data)).convert("RGB")
    ImageDraw.Draw(image).rectangle(bounds, fill="white")
    stream = BytesIO()
    image.save(stream, format="BMP")
    return stream.getvalue()


def test_decorative_model_changes_do_not_hide_any_content_or_renew_facts(tmp_path):
    from plugin.plugins.hsr_companion.runtime.patrol import remember_capture, should_scan
    owner, data = static_owner(tmp_path)
    original = copy.deepcopy(owner._committed_observation)

    async def check():
        await remember_capture(owner, data)
        for seq in (1, 2):
            result = await should_scan(owner, altered(data, (260, 100, 360, 300)),
                requested=True, changed=False, frame_seq=seq, captured_at=time.time(), pid=1)
        assert result is False
        assert owner._committed_observation == original
        assert owner._last_full_commit_serial == 4
    asyncio.run(check())


def test_unread_row_change_is_detected_before_first_stable_frame(tmp_path):
    from plugin.plugins.hsr_companion.runtime.patrol import remember_capture, should_scan
    owner, data = static_owner(tmp_path)

    async def check():
        await remember_capture(owner, data)
        # Outside the existing stat evidence; still inside the whole info pane.
        value = altered(data, (500, 250, 502, 251))
        assert await should_scan(owner, value, requested=False, changed=False,
            frame_seq=1, captured_at=time.time(), pid=1)
        assert owner._patrol_anchor["challenged"]
        assert await should_scan(owner, data, requested=True, changed=False,
            frame_seq=2, captured_at=time.time(), pid=1)
    asyncio.run(check())


def test_same_frame_or_stale_capture_cannot_establish_stability(tmp_path):
    from plugin.plugins.hsr_companion.runtime.patrol import remember_capture, should_scan
    owner, data = static_owner(tmp_path)

    async def check():
        await remember_capture(owner, data)
        for _ in range(3):
            assert await should_scan(owner, data, requested=True, changed=False,
                frame_seq=1, captured_at=time.time(), pid=1)
        assert await should_scan(owner, data, requested=True, changed=False,
            frame_seq=2, captured_at=time.time()-2.1, pid=1)
        assert not owner._patrol_anchor["stable"]
    asyncio.run(check())


def test_manual_followup_and_missing_coverage_still_get_full(tmp_path, monkeypatch):
    from plugin.plugins.hsr_companion.runtime import patrol
    from plugin.plugins.hsr_companion.runtime.patrol import remember_capture, should_scan
    owner, data = static_owner(tmp_path)
    origin, tick = time.time(), [0.]
    # Distinct capture frames need distinct timestamps even on coarse wall clocks.
    monkeypatch.setattr(patrol, "time", SimpleNamespace(time=lambda: origin + tick[0],
                                                      monotonic=lambda: 100. + tick[0]))

    async def call(seq):
        tick[0] = seq * .01
        return await should_scan(owner, data, requested=True, changed=False,
            frame_seq=seq, captured_at=origin + tick[0], pid=1)

    async def check():
        await remember_capture(owner, data)
        await call(1)
        assert not await call(2)
        owner._manual_refresh_task = SimpleNamespace(done=lambda: False)
        assert await call(3)
        owner._manual_refresh_task = None
        owner._pending_answer = object()
        assert await call(4)
        owner._pending_answer = None
        owner._visual_query_waiters = 1
        assert await call(5)
        owner._region_query_waiters = 1
        assert not await call(6)  # Already reading on demand: do not create another full.
        owner._visual_query_waiters = owner._region_query_waiters = 0
        owner._committed_observation["screen_facts"]["facts"][0]["evidence"] = []
        assert await call(7)
    asyncio.run(check())


def test_new_object_or_lifecycle_cannot_use_the_old_anchor(tmp_path):
    from plugin.plugins.hsr_companion.runtime.patrol import remember_capture, should_scan
    owner, data = static_owner(tmp_path)

    async def check():
        await remember_capture(owner, data)
        for seq in (1, 2):
            await should_scan(owner, data, requested=True, changed=False,
                frame_seq=seq, captured_at=time.time(), pid=1)
        for attribute in ("_observation_epoch", "_capture_failure_revision", "_last_full_commit_serial"):
            previous = getattr(owner, attribute, 0)
            setattr(owner, attribute, previous+1)
            assert await should_scan(owner, data, requested=True, changed=False,
                frame_seq=3, captured_at=time.time(), pid=1)
            setattr(owner, attribute, previous)
        assert await should_scan(owner, data, requested=True, changed=False,
            frame_seq=4, captured_at=time.time(), pid=2)
        assert await should_scan(owner, altered(data, (515, 40, 530, 50)),
            requested=False, changed=False, frame_seq=5, captured_at=time.time(), pid=1)
    asyncio.run(check())


def test_any_known_information_inside_model_area_remains_protected(tmp_path):
    from plugin.plugins.hsr_companion.runtime.patrol import remember_capture, should_scan
    owner, data = static_owner(tmp_path)
    owner._committed_observation["screen_facts"]["facts"][0]["evidence"][0]["region"] = {
        "left": .43, "top": .25, "right": .57, "bottom": .65}

    async def check():
        await remember_capture(owner, data)
        assert await should_scan(owner, altered(data, (300, 150, 310, 170)),
            requested=False, changed=False, frame_seq=1, captured_at=time.time(), pid=1)
    asyncio.run(check())
