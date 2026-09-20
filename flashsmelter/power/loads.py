"""负荷分级与设备负荷台账。

供电系统按四级管理厂内负荷，级别同时决定两件事：失电时的切除顺序，以及复电时
的送电顺序与自启动批次。晃电（短时电压暂降）与真正停电区别处理：

* ``security`` 保安负荷（一级）：晃电期间不允许失电，由 UPS/直流母线顶住；停电
  后由柴油发电机经保安母线继续供电，例如 DCS、应急照明、事故风机、冷却水泵。
* ``critical`` 重要负荷（二级）：晃电时立即跳开，复电后按 ``sag_group`` 分批
  自启动（同一批次同时送，批次之间留延时，避免多台电机同时再启动冲击母线）。
* ``normal`` 一般负荷（三级）：停电即切除，复电顺序中排在重要负荷之后。
* ``optional`` 可拉闸负荷（四级）：失电最先切除，复电最后送电，必要时可人工
  长期停用。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ValidationError

# 负荷级别：序号即优先级，越小越重要。
GRADES: tuple[str, ...] = ("security", "critical", "normal", "optional")

GRADE_LABELS: Mapping[str, str] = {
    "security": "保安（一级）",
    "critical": "重要（二级）",
    "normal": "一般（三级）",
    "optional": "可拉闸（四级）",
}

# 晃电（电压暂降）处置策略。
SAG_POLICIES: tuple[str, ...] = ("keep", "restart", "manual")

SAG_POLICY_LABELS: Mapping[str, str] = {
    "keep": "晃电保持（UPS/直流/机械惯性能维持）",
    "restart": "晃电后按批次自启动",
    "manual": "晃电后必须人工确认送电",
}

# 设备由哪一路电源供电，决定停电期间谁能被柴油发电机带上。
SOURCES: tuple[str, ...] = ("ups", "genset", "grid")

# 送电状态。
ENERGIZED_STATES: tuple[str, ...] = ("energized", "de-energized", "blocked")

# 失电切除顺序：可拉闸 → 一般 → 重要；保安负荷单独走发电机衔接逻辑。
SHED_ORDER: tuple[str, ...] = ("optional", "normal", "critical")

# 默认负荷台账：一条闪速炉产线的典型配电构成。
# restore_order 为全厂复电顺序（含保安负荷从发电机切回市电的重送），数字越小
# 越早送电；sag_group 仅对 restart 策略有意义，1 最早自启动。
DEFAULT_LOADS: tuple[Mapping[str, Any], ...] = (
    {
        "device_id": "emergency-lighting",
        "name": "应急照明",
        "grade": "security",
        "kw": 15.0,
        "sag_policy": "keep",
        "source": "ups",
        "sag_group": 0,
        "restore_order": 10,
    },
    {
        "device_id": "dcs-control-power",
        "name": "DCS 与控制电源",
        "grade": "security",
        "kw": 40.0,
        "sag_policy": "keep",
        "source": "ups",
        "sag_group": 0,
        "restore_order": 20,
    },
    {
        "device_id": "burner-plc-io",
        "name": "燃烧器 PLC 与现场 IO",
        "grade": "security",
        "kw": 20.0,
        "sag_policy": "keep",
        "source": "ups",
        "sag_group": 0,
        "restore_order": 30,
    },
    {
        "device_id": "cooling-water-pump-a",
        "name": "冷却水泵 A（炉体与烟罩）",
        "grade": "security",
        "kw": 110.0,
        "sag_policy": "keep",
        "source": "genset",
        "sag_group": 0,
        "restore_order": 40,
    },
    {
        "device_id": "boiler-feed-pump",
        "name": "余热锅炉给水泵",
        "grade": "security",
        "kw": 90.0,
        "sag_policy": "keep",
        "source": "genset",
        "sag_group": 0,
        "restore_order": 50,
    },
    {
        "device_id": "emergency-vent-fan",
        "name": "事故排烟风机",
        "grade": "security",
        "kw": 75.0,
        "sag_policy": "keep",
        "source": "genset",
        "sag_group": 0,
        "restore_order": 60,
    },
    {
        "device_id": "instrument-air-compressor",
        "name": "仪表空压机",
        "grade": "security",
        "kw": 55.0,
        "sag_policy": "keep",
        "source": "genset",
        "sag_group": 0,
        "restore_order": 70,
    },
    {
        "device_id": "asu-oxygen-compressor",
        "name": "空分氧压机",
        "grade": "critical",
        "kw": 900.0,
        "sag_policy": "restart",
        "source": "grid",
        "sag_group": 3,
        "restore_order": 600,
    },
    {
        "device_id": "concentrate-feeder",
        "name": "精矿喷吹螺旋给料",
        "grade": "critical",
        "kw": 130.0,
        "sag_policy": "restart",
        "source": "grid",
        "sag_group": 2,
        "restore_order": 400,
    },
    {
        "device_id": "forced-draft-fan",
        "name": "助燃风机",
        "grade": "critical",
        "kw": 220.0,
        "sag_policy": "restart",
        "source": "grid",
        "sag_group": 1,
        "restore_order": 300,
    },
    {
        "device_id": "esp-rectifier",
        "name": "电收尘整流机组",
        "grade": "critical",
        "kw": 180.0,
        "sag_policy": "restart",
        "source": "grid",
        "sag_group": 3,
        "restore_order": 610,
    },
    {
        "device_id": "concentrate-belt",
        "name": "精矿上料皮带",
        "grade": "normal",
        "kw": 75.0,
        "sag_policy": "restart",
        "source": "grid",
        "sag_group": 4,
        "restore_order": 800,
    },
    {
        "device_id": "ball-mill",
        "name": "湿煤球磨机",
        "grade": "normal",
        "kw": 260.0,
        "sag_policy": "manual",
        "source": "grid",
        "sag_group": 0,
        "restore_order": 900,
    },
    {
        "device_id": "slag-granulation-pump",
        "name": "渣浆粒化泵",
        "grade": "normal",
        "kw": 100.0,
        "sag_policy": "restart",
        "source": "grid",
        "sag_group": 4,
        "restore_order": 820,
    },
    {
        "device_id": "matte-ladle-crane",
        "name": "冰铜吊行车",
        "grade": "optional",
        "kw": 140.0,
        "sag_policy": "manual",
        "source": "grid",
        "sag_group": 0,
        "restore_order": 1000,
    },
)


def validate_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """校验并归一化一条负荷登记参数。"""

    device_id = str(spec.get("device_id", "")).strip()
    if not device_id:
        raise ValidationError("缺少设备编号", details={"param": "device_id"})
    if len(device_id) > 48:
        raise ValidationError("设备编号超长", details={"device_id": device_id, "max": 48})
    name = str(spec.get("name", "")).strip()
    if not name:
        raise ValidationError("缺少设备名称", details={"device_id": device_id})
    grade = str(spec.get("grade", "")).strip()
    if grade not in GRADES:
        raise ValidationError(
            "负荷级别不合法",
            details={"device_id": device_id, "grade": grade, "allowed": list(GRADES)},
        )
    sag_policy = str(spec.get("sag_policy", "")).strip()
    if sag_policy not in SAG_POLICIES:
        raise ValidationError(
            "晃电策略不合法",
            details={"device_id": device_id, "sag_policy": sag_policy, "allowed": list(SAG_POLICIES)},
        )
    source = str(spec.get("source", "")).strip()
    if source not in SOURCES:
        raise ValidationError(
            "供电来源不合法",
            details={"device_id": device_id, "source": source, "allowed": list(SOURCES)},
        )
    try:
        kw = float(spec.get("kw"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("设备容量必须是数值", details={"device_id": device_id}) from exc
    if kw != kw or kw <= 0:
        raise ValidationError("设备容量必须为正数", details={"device_id": device_id, "kw": kw})
    try:
        sag_group = int(spec.get("sag_group", 0))
    except (TypeError, ValueError) as exc:
        raise ValidationError("自启动批次必须是整数", details={"device_id": device_id}) from exc
    if sag_group < 0 or sag_group > 99:
        raise ValidationError(
            "自启动批次超出 [0,99]", details={"device_id": device_id, "sag_group": sag_group}
        )
    if sag_policy == "restart" and sag_group == 0:
        raise ValidationError(
            "晃电自启动设备必须指定 sag_group >= 1 的批次",
            details={"device_id": device_id},
        )
    if sag_policy != "restart" and sag_group != 0:
        raise ValidationError(
            "只有 restart 策略允许设置非零自启动批次",
            details={"device_id": device_id, "sag_policy": sag_policy},
        )
    try:
        restore_order = int(spec.get("restore_order"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("复电顺序必须是整数", details={"device_id": device_id}) from exc
    if not 1 <= restore_order <= 10000:
        raise ValidationError(
            "复电顺序必须在 [1,10000]", details={"device_id": device_id, "restore_order": restore_order}
        )
    # 保安负荷晃电期间必须有 UPS/直流支撑，或在停电后由发电机带上。
    if grade == "security" and sag_policy != "keep":
        raise ValidationError(
            "保安负荷的晃电策略必须是 keep",
            details={"device_id": device_id, "grade": grade, "sag_policy": sag_policy},
        )
    if grade == "security" and source == "grid":
        raise ValidationError(
            "保安负荷必须挂在 UPS 或发电机保安母线上",
            details={"device_id": device_id, "source": source},
        )
    if grade != "security" and source in ("ups", "genset"):
        raise ValidationError(
            "只有保安负荷允许挂 UPS/发电机母线",
            details={"device_id": device_id, "grade": grade, "source": source},
        )
    if grade != "security" and sag_policy == "keep":
        raise ValidationError(
            "只有保安负荷允许使用晃电保持策略",
            details={"device_id": device_id, "grade": grade, "sag_policy": sag_policy},
        )
    return {
        "device_id": device_id,
        "name": name,
        "grade": grade,
        "kw": round(kw, 3),
        "sag_policy": sag_policy,
        "source": source,
        "sag_group": sag_group,
        "restore_order": restore_order,
    }


def default_catalog() -> list[dict[str, Any]]:
    """默认台账（深拷贝，调用方可自由修改）。"""

    return [dict(item) for item in DEFAULT_LOADS]


__all__ = [
    "GRADES",
    "GRADE_LABELS",
    "SAG_POLICIES",
    "SAG_POLICY_LABELS",
    "SOURCES",
    "ENERGIZED_STATES",
    "SHED_ORDER",
    "DEFAULT_LOADS",
    "validate_spec",
    "default_catalog",
]
