"""Canonical AgentNetwork interchange contract with legacy 0.1 compatibility."""
import datetime
import json
import re

VERSION = "0.2.0"
LEGACY_VERSION = "0.1.0"
KNOWLEDGE_TYPES = {"observation", "procedure", "fact", "decision", "constraint", "failed_attempt", "result", "other"}
_SPACE = re.compile(r"(?:pkg:[^\s]{1,500}|repo:https://[^\s]{1,490}|(?:domain|topic|entity):[a-z0-9][a-z0-9._:/-]{0,490})\Z")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,119}\Z")


def _string_list(value, field, *, limit=32, item_limit=120):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Invalid " + field)
    result=[]
    for item in value:
        if not isinstance(item,str) or not item.strip() or len(item)>item_limit or item!=item.strip():
            raise ValueError("Invalid " + field)
        result.append(item)
    if len(set(x.casefold() for x in result)) != len(result):
        raise ValueError("Duplicate " + field)
    return result


def _applicability(value):
    if not isinstance(value,dict) or len(value)>24 or len(json.dumps(value,ensure_ascii=False))>4096:
        raise ValueError("Invalid applicability")
    for key,item in value.items():
        if not isinstance(key,str) or not _TOKEN.fullmatch(key):
            raise ValueError("Invalid applicability key")
        values=item if isinstance(item,list) else [item]
        if isinstance(item,list) and len(item)>32:
            raise ValueError("Invalid applicability value")
        for entry in values:
            if isinstance(entry,str):
                if len(entry)>256:raise ValueError("Invalid applicability value")
            elif not isinstance(entry,(int,float,bool)) or isinstance(entry,float) and (entry!=entry or abs(entry)==float("inf")):
                raise ValueError("Invalid applicability value")
    return value


def validate_record(data):
    base={"schema_version", "idempotency_key", "title", "evidence", "space", "observed_at", "source_visibility", "provenance", "environment"}
    modern={"domain","knowledge_type","subjects","tags","applicability"}
    if not isinstance(data,dict) or not base<=data.keys():
        raise ValueError("Record fields do not match contract")
    version=data["schema_version"]
    if version not in (LEGACY_VERSION,VERSION):
        raise ValueError("Unsupported version")
    allowed=base|({"supersedes"} if version==LEGACY_VERSION else modern|{"supersedes"})
    if data.keys()-allowed or (version==VERSION and not modern<=data.keys()):
        raise ValueError("Record fields do not match contract")
    if data["source_visibility"] != "public":
        raise ValueError("Only explicitly public source is supported")
    for key, minimum, maximum in (("idempotency_key",8,128),("title",3,200),("evidence",10,16000),("space",4,512),("observed_at",10,40)):
        value=data[key]
        if not isinstance(value,str) or not minimum<=len(value)<=maximum or version==VERSION and value!=value.strip():
            raise ValueError("Invalid " + key)
    if not _SPACE.fullmatch(data["space"]):
        raise ValueError("Use a package, repository, domain, topic, or entity identifier")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+",data["idempotency_key"]):
        raise ValueError("Invalid idempotency key")
    try:date=datetime.datetime.fromisoformat(data["observed_at"].replace("Z","+00:00"))
    except ValueError as exc:raise ValueError("Invalid observed_at") from exc
    if date.tzinfo is None or date > datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=5):
        raise ValueError("Observation time must include timezone and not be in the future")
    p=data["provenance"]
    if not isinstance(p,dict) or set(p)!={"kind","derived_from"} or p["kind"] not in ("direct_observation","synthesis"):
        raise ValueError("Invalid provenance")
    if not isinstance(p["derived_from"],list) or len(p["derived_from"])>20 or any(not isinstance(x,str) or len(x)>128 for x in p["derived_from"]):
        raise ValueError("Invalid source references")
    if p["kind"]=="synthesis" and not p["derived_from"]:
        raise ValueError("Synthesis requires source references")
    env=data["environment"]
    if not isinstance(env,dict) or set(env)-{"package_version","os","runtime"} or any(not isinstance(v,str) or len(v)>100 for v in env.values()):
        raise ValueError("Invalid environment")
    if data.get("supersedes") is not None and not re.fullmatch(r"[0-9a-f-]{36}",data["supersedes"]):
        raise ValueError("Invalid superseded revision")
    if version==VERSION:
        if not isinstance(data["domain"],str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}",data["domain"]):raise ValueError("Invalid domain")
        if not isinstance(data["knowledge_type"],str) or data["knowledge_type"] not in KNOWLEDGE_TYPES:raise ValueError("Invalid knowledge_type")
        _string_list(data["subjects"],"subjects")
        _string_list(data["tags"],"tags")
        _applicability(data["applicability"])
    return data


def normalized_record(data):
    """Return the current representation while retaining legacy records."""
    validate_record(data)
    if data["schema_version"]==VERSION:
        d=dict(data)
        d["subjects"]=[x.casefold() for x in data["subjects"]]
        d["tags"]=[x.casefold() for x in data["tags"]]
        return d
    d=dict(data)
    d.update(schema_version=VERSION,
        domain="software" if data["space"].startswith(("pkg:","repo:")) else "general",
        knowledge_type="observation",subjects=[],tags=[],applicability=dict(data["environment"]))
    return d
