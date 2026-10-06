"""「姿势控制」的正运动学与投影：把 URDF 关节角算成示意图上的点。

从 `core_motion.py` 拆出来的（v6.0.0 加了实时流之后那个文件到 1099 行，过了本项目的
"单文件 1000 行"红线）。**这里是一段自洽的纯数学**：只依赖 `math` / `typing`，
不碰配置、不碰下发、不发任何指令 —— 所以它能独立单测，也能单独看：

* `fk_trunk` / `fk_arm`：URDF 正运动学（参数逐条可复查，见下面的链表注释）；
* `project_side` / `project_front`：三维点 → 二维画面（侧视 / 正视）；
* `fit_projection` / `map_point`：等比缩放到画布（纯函数，可单测）。

`core_motion` 仍然把这些名字**再导出**一次，所以 `core_motion.fk_trunk` 的写法与旧代码
（以及测试里的 patch 口径）都不变。界面那一层看 `ui/posture_draw.py`。
"""

from typing import Sequence, Tuple

import math


# ============ 正运动学（示意图用） ============
#: 每一段是 ``(偏置 xyz(米), 旋转轴 或 None, 角度来源轴名 或 None, 说明)``。
#:
#: 为什么干脆做正运动学：v5.6.0 的躯干示意图是"三段等长连杆交替折返"的手画简图 ——
#: 现场一眼就指出"躯干不是这样"。工作空间里有 URDF，算出来的形状就是真机形状，
#: 而且**不再需要靠试符号**去猜方向。参数逐条可复查：
#: `install/share/robot_description/urdf/wheel_robot.urdf`（躯干）与
#: `install/share/product/products/NERV-AMA01/robot_description/urdf/wheel_robot.urdf`（AR5）。
#:
#: 坐标系：`chassis_base_link` 为原点，**+X 前 / +Y 左 / +Z 上**（REP-103）。
TRUNK_CHAIN = (
    ((-0.0315, 0.0, 0.262), None, None),          # chassis → vtrunk_base（底座上方）
    ((0.0, 0.0535, 0.112), (0, -1, 0), 0),        # trunk1_joint   H1 脚踝
    ((0.0, -0.01, 0.287), (0, 1, 0), 1),          # trunk2_joint   H2 膝
    ((0.0005, 0.0, 0.295), (0, -1, 0), 2),        # trunk3_joint   H3 大腿
    ((0.0, -0.044007, 0.099), (0, 0, 1), 3),      # trunk4_joint   H4 腰（绕 Z）
    ((0.0, 0.0, 0.4885), (0, 0, 1), 4),           # head1_joint    H5 颈 · 转头（躯干段）
    ((0.0, 0.0213, 0.041), (0, -1, 0), 5),        # head2_joint    H6 颈 · 抬头
)
#: AR5 七轴：``(偏置 xyz(米), 旋转轴)`` —— 顺序就是下发数组顺序 J1…J7
ARM_CHAIN = (
    ((0.0, 0.0, 0.08725), (0, 0, 1)),             # J1 肩（偏航）
    ((0.0, 0.0, 0.08725), (0, 1, 0)),             # J2 肩（俯仰）
    ((0.0, 0.0, 0.314), (0, 0, 1)),               # J3 肩（滚转）—— 上臂 0.314m
    ((0.0105, 0.0, 0.0), (0, 1, 0)),              # J4 肘
    ((-0.0105, 0.0, 0.272), (0, 0, 1)),           # J5 腕 —— 前臂 0.272m
    ((0.0, 0.0, 0.0), (0, 1, 0)),                 # J6 腕
    ((0.0, 0.0, 0.0), (1, 0, 0)),                 # J7 腕
)
#: 手臂挂点（在 **trunk4_link（腰）** 坐标系里）：左 0.067 在 +Y、右对称；rpy 是 ∓90° 绕 X
ARM_MOUNTS = {"left": ((0.0, 0.067, 0.413504718725029), (-1.5707963267949, 0.0, 0.0)),
              "right": ((0.0, -0.067, 0.413504718725028), (1.5707963267949, 0.0, 0.0))}
#: 手掌长度（腕 → 指尖，`left_hand_end_joint` 的偏置）
HAND_LENGTH = 0.1432
_TWO_PI = 6.283185307179586


def _rot3(axis, angle: float):
    """绕单位轴转 ``angle`` 的 3×3 旋转矩阵（右手定则）。"""
    x, y, z = axis
    norm = (x * x + y * y + z * z) ** 0.5 or 1.0
    x, y, z = x / norm, y / norm, z / norm
    cos_a, sin_a, one = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
    return [[x * x * one + cos_a, x * y * one - z * sin_a, x * z * one + y * sin_a],
            [y * x * one + z * sin_a, y * y * one + cos_a, y * z * one - x * sin_a],
            [z * x * one - y * sin_a, z * y * one + x * sin_a, z * z * one + cos_a]]


def _mul3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rpy3(roll: float, pitch: float, yaw: float):
    """URDF 的 `rpy` = ``Rz(yaw) · Ry(pitch) · Rx(roll)``。"""
    return _mul3(_mul3(_rot3((0, 0, 1), yaw), _rot3((0, 1, 0), pitch)),
                 _rot3((1, 0, 0), roll))


def _apply3(matrix, vector):
    return tuple(sum(matrix[i][k] * vector[k] for k in range(3)) for i in range(3))


class _Chain:
    """把一串关节串起来，记下每个关节的落点。

    ⚠️ URDF 语义：`<origin xyz rpy>` 是"**先在父坐标系里平移 xyz，再按 rpy 转到子坐标系**"。
    先转后移会把坐标系搞错（手臂上表现得很明显：肩会"往外伸 0.34m"）。
    """

    def __init__(self):
        self.matrix = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
        self.points = [(0.0, 0.0, 0.0)]

    def push(self, xyz, rpy_=None, axis=None, angle: float = 0.0):
        moved = _apply3(self.matrix, xyz)
        self.points.append(tuple(self.points[-1][i] + moved[i] for i in range(3)))
        if rpy_ is not None:
            self.matrix = _mul3(self.matrix, _rpy3(*rpy_))
        if axis is not None:
            self.matrix = _mul3(self.matrix, _rot3(axis, angle))
        return self.points[-1]


def fk_trunk(values_deg: Sequence[float]) -> dict:
    """躯干正运动学：返回侧视/正视都能用的三维点。

    Returns:
        ``{"base", "ankle", "knee", "thigh", "hip", "neck", "head", "points", "hip_xyz",
        "yaw_deg"}`` —— `points` 是画折线用的序列（底座上方 → H6），`hip_xyz` 是腰的位置
        （手臂挂点的参考原点，画手臂时要加它）。
    """
    values = [float(item) for item in values_deg] + [0.0] * len(TRUNK_CHAIN)
    chain = _Chain()
    for offset, axis, index in TRUNK_CHAIN:
        angle = math.radians(values[index]) if index is not None else 0.0
        chain.push(offset, None, axis, angle)
    (_origin, base, ankle, knee, thigh, hip, neck, head) = chain.points
    return {"base": base, "ankle": ankle, "knee": knee, "thigh": thigh, "hip": hip,
            "neck": neck, "head": head,
            "points": [base, ankle, knee, thigh, hip, neck, head],
            "hip_xyz": hip, "yaw_deg": values[3]}


def fk_arm(side: str, values_deg: Sequence[float], yaw_deg: float = 0.0) -> dict:
    """一条手臂的正运动学（在**腰坐标系**里）。

    Args:
        side: ``"left"`` / ``"right"``。
        values_deg: 7 个**度**。
        yaw_deg: 腰的偏航（H4）—— 躯干转了，肩的朝向跟着转，画正视时要一起转。

    Returns:
        ``{"shoulder", "elbow", "wrist", "tip", "points"}``（腰坐标系，单位米）。
    """
    mount_xyz, mount_rpy = ARM_MOUNTS[side]
    values = [float(item) for item in values_deg] + [0.0] * len(ARM_CHAIN)
    chain = _Chain()
    chain.push(mount_xyz, mount_rpy)
    for step, ((offset, axis), index) in enumerate(zip(ARM_CHAIN, range(len(ARM_CHAIN)))):
        chain.push(offset, None, axis, math.radians(values[index]))
    shoulder, elbow, wrist = chain.points[3], chain.points[5], chain.points[8]
    chain.push((0.0, 0.0, 0.0), (math.pi / 2, 0.0, 0.0))
    chain.push((0.0, HAND_LENGTH, 0.0), (-math.pi / 2, 0.0, 0.0))
    tip = chain.points[-1]
    yaw = math.radians(yaw_deg % 360.0)
    rotate = _rot3((0, 0, 1), yaw)
    out = {}
    for name, point in (("shoulder", shoulder), ("elbow", elbow),
                        ("wrist", wrist), ("tip", tip)):
        out[name] = _apply3(rotate, point)
    out["points"] = [out["shoulder"], out["elbow"], out["wrist"], out["tip"]]
    return out


def project_side(point) -> Tuple[float, float]:
    """**侧视**投影（站在机器人右侧看，机头朝左）：``(x, y, z) → (-x, -z)``。"""
    return (-point[0], -point[2])


def project_front(point) -> Tuple[float, float]:
    """**正视**投影（从机器人正面看，机器人左臂落在画面右侧）：``(x, y, z) → (y, -z)``。"""
    return (point[1], -point[2])


def fit_projection(points_2d, width: float, height: float, margin: float) -> dict:
    """把一组二维点等比缩放到画布内（含居中），返回 ``{scale, ox, oy}``。

    抽成纯函数是为了让"图到底画在哪"可单测：给一组点与画布，缩放/平移必须是确定的。
    """
    if not points_2d:
        return {"scale": 1.0, "ox": 0.0, "oy": 0.0}
    xs = [item[0] for item in points_2d]
    ys = [item[1] for item in points_2d]
    span_x = max(max(xs) - min(xs), 1e-6)
    span_y = max(max(ys) - min(ys), 1e-6)
    usable_x = max(width - 2 * margin, 1e-6)
    usable_y = max(height - 2 * margin, 1e-6)
    scale = min(usable_x / span_x, usable_y / span_y)
    ox = margin + (usable_x - span_x * scale) / 2 - min(xs) * scale
    oy = margin + (usable_y - span_y * scale) / 2 - min(ys) * scale
    return {"scale": scale, "ox": ox, "oy": oy}


def map_point(point_2d, fitted) -> Tuple[float, float]:
    return (point_2d[0] * fitted["scale"] + fitted["ox"],
            point_2d[1] * fitted["scale"] + fitted["oy"])
