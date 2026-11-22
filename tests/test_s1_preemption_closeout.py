"""S1 follow-up: native-call preemption and same-frame compact acquisition."""
import asyncio
import threading
from io import BytesIO

import numpy as np
import pytest
from PIL import Image, ImageDraw
from plugin.plugins.hsr_companion.adapters.native_text import QueryPreempted, read_native_text
from plugin.plugins.hsr_companion.detectors.character_regions import read_character_detail
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture

# Solid markers stand for glyphs; their positions survive cropping and atlases.
TOKENS = [
    ("\u89d2\u8272\u8be6\u60c5", (240, 30, 30), (.05, .035, .13, .065)),
    ("\u5149\u9525", (30, 240, 30), (.112, .202, .145, .23)),
    ("\u884c\u8ff9", (30, 30, 240), (.112, .352, .145, .385)),
    ("\u94f6\u72fc", (220, 180, 30), (.78, .11, .86, .15)),
    ("irrelevant-center", (210, 30, 180), (.38, .43, .48, .48)),
    ("\u9632\u5fa1\u529b", (30, 190, 190), (.66, .43, .72, .46)),
    ("3335", (200, 100, 30), (.85, .43, .91, .46)),
]


def picture(*, role="\u94f6\u72fc", title="\u89d2\u8272\u8be6\u60c5", controls=True, size=(2560,1600), two_names=False):
    image=Image.new("RGB",size,(10,15,20))
    draw=ImageDraw.Draw(image)
    for index,(_,colour,bounds) in enumerate(TOKENS):
        if not controls and index<3:
            continue
        x1,y1,x2,y2=bounds
        draw.rectangle((int(x1*size[0]),int(y1*size[1]),int(x2*size[0]),int(y2*size[1])),fill=colour)
    if two_names:
        draw.rectangle((int(.90*size[0]),int(.11*size[1]),int(.97*size[0]),int(.15*size[1])),fill=(130,70,210))
    stream=BytesIO()
    image.save(stream,format="PNG")
    return stream.getvalue()


class PixelBackend:
    def __init__(self, *, role="\u94f6\u72fc", title="\u89d2\u8272\u8be6\u60c5", score=.99, disagree=False):
        self.role,self.title,self.score,self.disagree=role,title,score,disagree
        self.calls=[]

    def extract_text_with_boxes(self,image):
        array=np.asarray(image.convert("RGB"))
        found=[]
        tokens=[*TOKENS,("\u82b1\u706b",(130,70,210),None)]
        for index,(text,colour,_) in enumerate(tokens):
            ys,xs=np.nonzero(np.all(array==colour,axis=2))
            if not len(xs):
                continue
            if index==0:
                text=self.title
            if index==3:
                text="\u82b1\u706b" if self.disagree and self.calls else self.role
            found.append(dict(text=text,score=self.score if index==3 else .99,
                left=int(xs.min()),top=int(ys.min()),right=int(xs.max())+1,bottom=int(ys.max())+1))
        self.calls.append({"size":image.size,"tokens":[b["text"] for b in found]})
        return "",found


def layout_hint():
    def region(bounds):
        return {"region":dict(zip(("left","top","right","bottom"),bounds))}
    return {"source_size":[2560,1600],"page_controls":[region(t[2]) for t in TOKENS[:3]],
            "name_regions":[region(TOKENS[3][2])]}


def test_query_already_waiting_preempts_before_initial_scene_probe():
    calls=[]
    class Backend:
        def extract_text_with_boxes(self,image):
            calls.append(image.size)
            return "",[]
    with pytest.raises(QueryPreempted):
        read_native_text(Backend(),Image.new("RGB",(2560,1600)),on_progress=lambda _:None,
                         yield_requested=lambda:True)
    assert calls==[]


def test_new_query_before_name_confirmation_backend_read_is_not_ignored():
    calls=[]
    demand=False
    class Backend:
        def extract_text_with_boxes(self,image):
            calls.append(image.size)
            return "",[]
    def verification(backend,image,boxes,catalog,*,cancelled,read):
        nonlocal demand
        demand=True
        read(Image.new("RGB",(100,30)),"name_verification")
        return boxes,{}
    with pytest.raises(QueryPreempted):
        read_native_text(Backend(),Image.new("RGB",(640,400)),catalog=object(),
            verify_names=True,name_labels=lambda _:[],skip_name_reread=lambda _:True,
            name_verifier=verification,yield_requested=lambda:demand)
    assert calls==[(640,400)]


@pytest.mark.parametrize("with_hint",[False,True])
def test_character_acquisition_never_reads_the_unasked_center(tmp_path,with_hint):
    owner,_,_=plugin_fixture(tmp_path)
    backend=PixelBackend()
    kwargs={"layout_regions":layout_hint()} if with_hint else {}
    result=read_character_detail(backend,picture(),owner._catalog,fields={"characters"},**kwargs)
    assert result and [a["entity_id"] for a in result["characters"]]==["character.silverwolf"]
    assert all("irrelevant-center" not in c["tokens"] for c in backend.calls)
    assert len(backend.calls)==2  # One native layout read, one independent enlarged name read.
    assert result["characters"][0]["name_validation"]["method"]=="native_and_enlarged_label_agree"
    assert result["recognition"]["source_size"]==[2560,1600]
    if with_hint:
        assert backend.calls[0]["size"][0]*backend.calls[0]["size"][1]<2560*1600*.08


def test_old_layout_is_search_geometry_not_old_identity(tmp_path):
    owner,_,_=plugin_fixture(tmp_path)
    hint=layout_hint()
    hint["name_regions"][0].update(name="\u94f6\u72fc",entity_id="character.silverwolf")
    result=read_character_detail(PixelBackend(role="\u82b1\u706b"),picture(role="\u82b1\u706b"),
        owner._catalog,fields={"characters"},layout_regions=hint)
    assert [a["entity_id"] for a in result["characters"]]==["character.sparkle"]


@pytest.mark.parametrize("with_hint",[False,True])
@pytest.mark.parametrize("mode",["no_controls","wrong_title","weak_name","disagree","two_names"])
def test_compact_acquisition_cannot_authorize_unproved_identity(tmp_path,mode,with_hint):
    owner,_,_=plugin_fixture(tmp_path)
    backend=PixelBackend(title="\u80cc\u5305" if mode=="wrong_title" else "\u89d2\u8272\u8be6\u60c5",
        score=.89 if mode=="weak_name" else .99,disagree=mode=="disagree")
    result=read_character_detail(backend,picture(controls=mode!="no_controls",two_names=mode=="two_names"),
        owner._catalog,fields={"characters"},layout_regions=layout_hint() if with_hint else None)
    assert result is None or result["characters"]==[]


@pytest.mark.parametrize("mode",["resolution","invalid_coordinates"])
def test_invalid_old_geometry_falls_back_to_current_layout(tmp_path,mode):
    owner,_,_=plugin_fixture(tmp_path)
    hint=layout_hint()
    if mode=="resolution":
        hint["source_size"]=[640,400]
    else:
        hint["name_regions"][0]["region"]["left"]=float("nan")
    result=read_character_detail(PixelBackend(),picture(),owner._catalog,fields={"characters"},layout_regions=hint)
    assert result["characters"][0]["entity_id"]=="character.silverwolf"


def test_native_call_in_progress_is_not_claimed_to_be_preemptible():
    entered,release=threading.Event(),threading.Event()
    calls=[]
    demand=False
    class Backend:
        def extract_text_with_boxes(self,image):
            calls.append(image.size)
            entered.set()
            assert release.wait(2)
            return "",[]
    def read():
        return read_native_text(Backend(),Image.new("RGB",(2560,1600)),on_progress=lambda _:None,
                                yield_requested=lambda:demand)
    async def scenario():
        nonlocal demand
        task=asyncio.create_task(asyncio.to_thread(read))
        try:
            assert await asyncio.to_thread(entered.wait,1)
            demand=True
            await asyncio.sleep(.02)
            assert not task.done()
            release.set()
            with pytest.raises(QueryPreempted):
                await task
            assert len(calls)==1
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(scenario())



def test_demand_arriving_during_probe_preparation_stops_before_native_call(monkeypatch):
    demand=False
    calls=[]
    original=Image.Image.thumbnail
    def preparing(image,*args,**kw):
        nonlocal demand
        result=original(image,*args,**kw)
        demand=True
        return result
    class Backend:
        def extract_text_with_boxes(self,image):
            calls.append(image.size)
            return "",[]
    monkeypatch.setattr(Image.Image,"thumbnail",preparing)
    with pytest.raises(QueryPreempted):
        read_native_text(Backend(),Image.new("RGB",(2560,1600)),on_progress=lambda _:None,
                         yield_requested=lambda:demand)
    assert calls==[]


def test_region_crossing_atlas_seams_cannot_create_a_subject(tmp_path):
    owner,_,_=plugin_fixture(tmp_path)
    class Crossing(PixelBackend):
        def extract_text_with_boxes(self,image):
            text,boxes=super().extract_text_with_boxes(image)
            if len(self.calls)==1:
                boxes.append(dict(text="\u82b1\u706b",score=.99,left=0,top=0,right=image.width,bottom=image.height))
            return text,boxes
    result=read_character_detail(Crossing(),picture(),owner._catalog,fields={"characters"})
    assert [a["entity_id"] for a in result["characters"]]==["character.silverwolf"]


def test_old_geometry_only_passes_coordinates_not_cached_facts(tmp_path):
    from plugin.plugins.hsr_companion.runtime.current_regions import _layout_regions
    owner,_,_=plugin_fixture(tmp_path)
    owner._committed_observation={"page":{"page_type":"character_detail"},
        "recognition":{"source_size":[2560,1600],"scene_regions":[
            {"text":"old control",**layout_hint()["page_controls"][0]}]},
        "characters":[{"identity_scope":"detail_subject","name":"old name","entity_id":"character.fake",
                       **layout_hint()["name_regions"][0]}],
        "screen_facts":{"facts":[{"value":"7777"}]}}
    result=_layout_regions(owner)
    assert result==layout_hint()|{"page_controls":[layout_hint()["page_controls"][0]]}
    assert "old name" not in str(result) and "7777" not in str(result)


def test_cancel_during_compact_native_read_never_starts_name_confirmation(tmp_path):
    from plugin.plugins.hsr_companion.adapters.native_text import ObservationSuperseded
    owner,_,_=plugin_fixture(tmp_path)
    backend=PixelBackend()
    with pytest.raises(ObservationSuperseded):
        read_character_detail(backend,picture(),owner._catalog,fields={"characters"},
                              cancelled=lambda:bool(backend.calls))
    assert len(backend.calls)==1
