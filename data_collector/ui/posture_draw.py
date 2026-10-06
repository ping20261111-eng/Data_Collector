"""「姿势控制」页面的**示意图**：全部走 URDF 正运动学（`core_motion.fk_trunk` / `fk_arm`）。

为什么另成一册：v5.6.0 的躯干图是手画的"三段等长连杆"，现场一眼指出"不对"；换成正运动学后
画法本身有 150 行左右（含缩放、投影、三种用途：卡片大图 / 确认框 / 预设卡迷你版），
与"界面怎么拼"是两件事，分开放才读得下去，也才不撞项目「单文件 1000 行」的红线。

三种用途共用同一组 `paint_trunk` / `paint_arm` 纯函数 ——
**同一份数据在两个地方画出不同形状**才是最容易让人误判的事（现场会照着确认框做决定）。

视角（两个常量，改这里就换视角，不用动几何）：
* 躯干 **侧视**（`project_side`）：站在机器人右侧看、机头朝左 —— 与现场那把 RViz 渲染一致。
  图底部有一条方向轴标着「前 / 后」（`_draw_axis`）：**+X（前）在画面左边**，
  现场不必靠记忆判断；
* 手臂 **正视 + 侧视并排**（现场要求）：正视看"张开多少 / 抬到哪"，侧视看"往身前伸多少" ——
  正视会把前后方向压掉，只看它就会漏掉"手在身前还是身后"。
"""
from PyQt5.QtCore import QPointF, QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QWidget

from .. import theme
from ..core_motion import (ARM_AXES, fit_projection, fk_arm, fk_trunk, map_point,
                           project_front, project_side)

#: 底盘外形（chassis_base_link 坐标系，米）：x0 / x1 / z0 / z1
CHASSIS_BOX = (-0.26, 0.24, 0.0, 0.155)
#: 各线宽（画布像素）
W_LEG = 5.0
W_TORSO = 17.0
W_ARM = 4.6
W_ARM_CURRENT = 3.6
JOINT_R = 3.4
#: 迷你版（预设卡）用的更细一档
W_MINI = 2.4
#: 侧视图底部留给「前 / 后」方向轴的横带（像素）。图本身按"高度 - 这条带"来缩放，
#: 所以标注不会压到机构上。
ORIENT_BAND = 18.0
#: 双臂图的两个视角（现场要求：正视 + 侧视）。第 4 项是底部方向轴的标注 ——
#: 正视看左右（`project_front` 把机器人左 +Y 画在画面右侧，所以**画面左＝右臂**），
#: 侧视看前后（画面左＝机头方向）。
ARM_VIEWS = (
    ("front", "正视 ｜ 张开 / 抬升", project_front, ("右", "左")),
    ("side", "侧视 ｜ 前伸 / 后收", project_side, ("前", "后")),
)


def _palette():
    return theme.palette(theme.active_mode())


def _qcolor(name_or_value: str) -> QColor:
    palette = _palette()
    return QColor(palette.get(name_or_value, name_or_value))


def trunk_figure(values_deg, arms=None):
    """躯干的画法所需三维点。

    Args:
        values_deg: 6 个度。
        arms: ``{"left": [7 度], "right": [7 度]}`` 或 None —— 只作**陪衬**（让图看起来
            像那台机器），不参与这条指令的目标。

    Returns:
        ``{"skeleton", "torso", "head", "neck", "hip", "chassis", "arms"}``
        （三维坐标，单位米）。
    """
    fk = fk_trunk(values_deg)
    hip = fk["hip"]
    out = {"skeleton": [fk["base"], fk["ankle"], fk["knee"], fk["thigh"], fk["hip"]],
           "torso": [fk["hip"], fk["neck"]], "head": fk["head"], "neck": fk["neck"],
           "hip": hip,
           "chassis": ((CHASSIS_BOX[0], 0.0, CHASSIS_BOX[2]),
                       (CHASSIS_BOX[1], 0.0, CHASSIS_BOX[3])),
           "arms": {}}
    for side in ("left", "right"):
        values = (arms or {}).get(side)
        if not values:
            continue
        points = fk_arm(side, values, fk["yaw_deg"])["points"]
        out["arms"][side] = [(p[0] + hip[0], p[1] + hip[1], p[2] + hip[2]) for p in points]
    return out


def arm_figure(trunk_deg, targets, currents=None):
    """手臂正视图所需三维点（腰的偏航会一起转，所以躯干值也要给）。

    Returns:
        ``{"torso", "head", "arms": {side: {"current": [...], "target": [...]}}}``
    """
    fk = fk_trunk(trunk_deg)
    hip = fk["hip"]
    yaw = fk["yaw_deg"]
    out = {"torso": [fk["hip"], fk["neck"]], "head": fk["neck"],
           "hip": hip, "arms": {}}
    for side in ("left", "right"):
        bundle = {}
        for label, source in (("current", currents or {}), ("target", targets or {})):
            values = source.get(side) if source else None
            if not values:
                continue
            points = fk_arm(side, values, yaw)["points"]
            bundle[label] = [(p[0] + hip[0], p[1] + hip[1], p[2] + hip[2])
                             for p in points]
        if bundle:
            out["arms"][side] = bundle
    return out


def trunk_bounds(figure):
    """一张躯干图要框住的所有点（含陪衬手臂）—— 缩放必须按它算，否则手会被切掉。"""
    points = list(figure["skeleton"]) + list(figure["chassis"])
    for side in figure["arms"].values():
        points += list(side)
    return points


def arm_bounds(figure):
    points = list(figure["torso"]) + [figure["head"]]
    for bundle in figure["arms"].values():
        for line in bundle.values():
            points += list(line)
    return points


def _stroke(painter, points_2d, color: QColor, width: float, dash: bool = False,
            joints: bool = True, joint_r: float = JOINT_R, alpha: int = 255):
    if len(points_2d) < 2:
        return
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    if dash:
        pen.setStyle(Qt.DashLine)
    color.setAlpha(alpha)
    pen.setColor(color)
    painter.setPen(pen)
    painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points_2d]))
    if joints:
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        for x, y in points_2d:
            painter.drawEllipse(QRectF(x - joint_r, y - joint_r, joint_r * 2, joint_r * 2))
        painter.setBrush(Qt.NoBrush)


def paint_trunk(painter: QPainter, rect: QRectF, current, target, arms=None,
                margin: float = 10.0, mini: bool = False) -> None:
    """把"当前（虚线）→ 目标（实线）"的躯干图画到 ``rect`` 里（侧视）。

    画的东西按"现场能不能认出这台机器"选：黑底盘 → 三段折返的腿 → 躯干壳 → 头（带朝向面罩）；
    手臂只作陪衬。**虚线的当前值读不到时只画实线**（不编一个当前的形状出来）。
    """
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(rect.topLeft())
    box = QRectF(0, 0, rect.width(), rect.height())
    target = list(target or [])
    if len(target) < 6:
        target = (target + [0.0] * 6)[:6]
    current = list(current) if current else None
    show_current = bool(current) and [round(v, 2) for v in current[:6]] != \
        [round(v, 2) for v in target[:6]]

    pieces = {"target": trunk_figure(target, arms)}
    if show_current:
        pieces["current"] = trunk_figure(current, arms)
    bounds = trunk_bounds(pieces["target"])
    if show_current:
        bounds += trunk_bounds(pieces["current"])
    band = 0.0 if mini else ORIENT_BAND      # 底部给方向轴留一条，别让标注压到机构上
    fitted = fit_projection([project_side(p) for p in bounds], box.width(),
                            box.height() - band, margin if not mini else margin * 0.5)

    def at(point):
        return map_point(project_side(point), fitted)

    painter.setPen(QPen(_qcolor("line"), 1))
    painter.drawLine(QPointF(8, box.height() - band - 6),
                     QPointF(box.width() - 8, box.height() - band - 6))

    chassis = pieces["target"]["chassis"]
    corners = [at((chassis[0][0], 0, chassis[0][2])), at((chassis[1][0], 0, chassis[1][2]))]
    left, right = sorted(item[0] for item in corners)
    top, bottom = sorted(item[1] for item in corners)
    painter.setPen(Qt.NoPen)
    painter.setBrush(_qcolor("text"))
    painter.drawRoundedRect(QRectF(left, top, right - left, bottom - top), 5, 5)

    order = (["current", "target"] if show_current else ["target"])
    for name in order:
        figure = pieces[name]
        is_target = name == "target"
        color = _qcolor("accent") if is_target else _qcolor("muted")
        width = W_LEG if not mini else W_MINI
        _stroke(painter, [at(p) for p in figure["skeleton"]], color, width,
                dash=not is_target, joint_r=JOINT_R * (0.7 if mini else 1.0))
        if is_target:
            _stroke(painter, [at(p) for p in figure["torso"]], color,
                    W_TORSO if not mini else W_TORSO * 0.5, joints=False)
            neck, head = at(figure["neck"]), at(figure["head"])
            painter.setPen(QPen(color, 2))
            painter.drawLine(QPointF(*neck), QPointF(*head))
            size = 9.0 if mini else 16.0
            painter.setPen(QPen(color, 1.6))
            painter.setBrush(_qcolor("surface"))
            painter.drawRoundedRect(QRectF(head[0] - size / 2, head[1] - size / 2,
                                           size, size), size * 0.4, size * 0.4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(head[0] - size / 2 + 1, head[1] - size / 2 + 2,
                                           size * 0.34, size * 0.62),
                                    size * 0.15, size * 0.15)
        for points in figure["arms"].values():
            _stroke(painter, [at(p) for p in points],
                    _qcolor("accent") if is_target else _qcolor("muted2"),
                    W_ARM if not mini else W_MINI, dash=not is_target, alpha=150)
    if not mini:
        _draw_axis(painter, box, "前", "后")
    painter.restore()


def _draw_axis(painter: QPainter, box: QRectF, left_text: str, right_text: str) -> None:
    """在画布底部画一条双向方向轴，标出"画面左右各是哪边"。

    **为什么必须标**（现场要求）：不标的话看图的人得靠记忆判断图里的左边是哪一边，
    而这张图正是拿来做动作决定的那张（确认框里就是它）。两个视角的答案还不一样：

    * 躯干 / 手臂**侧视**（`project_side`）：站在机器人右侧看、机头朝左 →
      画面左 = **前**、画面右 = **后**；
    * 手臂**正视**（`project_front`）：从正前方看 → 画面左 = 机器人的**右臂**、右 = 左臂。

    这些方向由 `project_*` 决定，`test_motion.py` 与 `test_scripts.py` 有用例钉住。
    """
    y = box.height() - 8.0
    color = _qcolor("muted2")
    painter.setPen(QPen(color, 1.0))
    painter.drawLine(QPointF(30.0, y), QPointF(box.width() - 30.0, y))
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([QPointF(22.0, y), QPointF(31.0, y - 4.0),
                                   QPointF(31.0, y + 4.0)]))
    painter.drawPolygon(QPolygonF([QPointF(box.width() - 22.0, y),
                                   QPointF(box.width() - 31.0, y - 4.0),
                                   QPointF(box.width() - 31.0, y + 4.0)]))
    font = painter.font()
    font.setPointSizeF(8.0)
    painter.setFont(font)
    painter.setPen(QPen(color, 1.0))
    painter.drawText(QPointF(34.0, y - 3.0), left_text)
    painter.drawText(QPointF(box.width() - 21.0, y - 3.0), right_text)


def _caption(painter: QPainter, box: QRectF, text: str) -> None:
    """左上角的小视角名（带一块底色，压在图上也能看清）。"""
    font = painter.font()
    font.setPointSizeF(8.0)
    painter.setFont(font)
    width = painter.fontMetrics().horizontalAdvance(text) + 10
    painter.setPen(Qt.NoPen)
    painter.setBrush(_qcolor("surface"))
    painter.drawRoundedRect(QRectF(box.left() + 2, box.top() + 2, width, 15), 5, 5)
    painter.setPen(QPen(_qcolor("muted"), 1.0))
    painter.drawText(QPointF(box.left() + 7, box.top() + 13), text)


def arm_view_boxes(box: QRectF, gap: float = 12.0):
    """把一块画布横着切成两个视角，返回 ``[(名字, 标题, 投影函数, 矩形, 轴标注), ...]``。

    纯函数（可单测）：两块**必须等宽、不重叠**，否则现场看到的是"两个姿势挤在一起"，
    比只看一个还难判断。轴标注跟着投影方向走（正视是左右、侧视是前后），写在 `ARM_VIEWS`。
    """
    half = (box.width() - gap) / 2.0
    out = []
    for index, (name, title, project, axis) in enumerate(ARM_VIEWS):
        left = box.left() + index * (half + gap)
        out.append((name, title, project,
                    QRectF(left, box.top(), half, box.height()), axis))
    return out


def paint_arm(painter: QPainter, rect: QRectF, trunk_deg, targets, currents=None,
              margin: float = 8.0, mini: bool = False) -> None:
    """把"当前（虚线）→ 目标（实线）"的双臂图画到 ``rect`` 里（**正视 + 侧视**并排）。

    现场要求"正视图 + 侧视图一起看，更清楚"—— 两个视角说的是不同的事：正视看**往两侧
    张开多少、抬到哪**，侧视看**往身前伸多少**；正视图会把前后方向压掉，只看它就会漏掉
    "手伸到身前还是身后"。两块各占一半、共用同一份关节角，底部各有一条方向轴。
    """
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(rect.topLeft())
    box = QRectF(0, 0, rect.width(), rect.height())
    figure = arm_figure(trunk_deg, targets, currents)
    for _name, title, project, sub, axis in arm_view_boxes(box):
        _paint_arm_view(painter, sub, figure, project, margin, mini, title, axis)
    painter.restore()


def _paint_arm_view(painter: QPainter, sub: QRectF, figure, project, margin: float,
                    mini: bool, title: str, axis) -> None:
    """画其中一个视角（两块共用这一份画法 —— 同一份数据在两块里不会是两个姿势）。"""
    painter.save()
    painter.translate(sub.left(), sub.top())
    box = QRectF(0, 0, sub.width(), sub.height())
    band = 0.0 if mini else 16.0             # 底部留给方向轴
    fitted = fit_projection([project(p) for p in arm_bounds(figure)],
                            box.width(), box.height() - band,
                            margin if not mini else margin * 0.5)

    def at(point):
        return map_point(project(point), fitted)

    torso = [at(p) for p in figure["torso"]]
    head = at(figure["head"])
    # 躯干与头在这里只是**参照**（这一页画的是手臂）：细虚线 + 小圆点。
    # 原来是 17px 的实心粗条 —— 侧视里"手臂贴着身体垂下"时目标会被它整个盖住，
    # 现场根本看不出目标在哪（实测就是只剩一个肩点）。
    pen = QPen(_qcolor("muted2"))
    pen.setWidthF(2.4 if not mini else 1.6)
    pen.setStyle(Qt.DashLine)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(*torso[0]), QPointF(*torso[1]))
    radius = 7.0 if not mini else 4.0
    painter.setPen(Qt.NoPen)
    painter.setBrush(_qcolor("muted2"))
    painter.drawEllipse(QRectF(head[0] - radius, head[1] - radius,
                               radius * 2, radius * 2))
    for bundle in figure["arms"].values():
        if bundle.get("current") and bundle.get("target") and \
                bundle["current"] != bundle["target"]:
            _stroke(painter, [at(p) for p in bundle["current"]], _qcolor("muted"),
                    W_ARM_CURRENT if not mini else W_MINI, dash=True,
                    joint_r=JOINT_R * 0.8)
        if bundle.get("target"):
            points = [at(p) for p in bundle["target"]]
            _stroke(painter, points, _qcolor("accent"),
                    W_ARM if not mini else W_MINI)
            if not mini:
                side = 9.0
                painter.setPen(Qt.NoPen)
                painter.setBrush(_qcolor("accent"))
                painter.drawRoundedRect(QRectF(points[-1][0] - side / 2,
                                               points[-1][1] - side / 2, side, side), 3, 3)
    if not mini:
        _caption(painter, box, title)
        _draw_axis(painter, box, axis[0], axis[1])
    painter.restore()


class TrunkDiagram(QWidget):
    """躯干侧视示意图（``core_motion.fk_trunk`` 算出来的真形状）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current = None
        self.target = [0.0] * 6
        self.arms = {}
        self.setMinimumSize(150, 170)

    def set_values(self, current, target, arms=None) -> None:
        self.current = list(current) if current else None
        self.target = list(target or [])
        self.arms = arms or {}
        self.update()

    def sizeHint(self):                                        # noqa: N802 (Qt 命名)
        # 躯干是"高瘦"的：给个竖着的默认尺寸，比硬塞成正方形少浪费一半宽度
        return QSize(190, 250)

    def paintEvent(self, _event):                              # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        paint_trunk(painter, QRectF(0, 0, self.width(), self.height()),
                    self.current, self.target, self.arms)
        painter.end()


class ArmDiagram(QWidget):
    """双臂示意图（``core_motion.fk_arm``）：**正视 + 侧视并排**。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trunk = [0.0] * 6
        self.targets = {}
        self.currents = {}
        self.setMinimumSize(180, 150)

    def set_values(self, trunk_deg, targets, currents=None) -> None:
        self.trunk = list(trunk_deg or [0.0] * 6)
        self.targets = dict(targets or {})
        self.currents = dict(currents or {})
        self.update()

    def sizeHint(self):                                        # noqa: N802 (Qt 命名)
        # 两个视角并排 → 宽度是单视角的两倍（230 一块）。
        # 注意：Qt 的元素比较里"最宽"才是尺寸提示里的宽，所以这里给的是**总宽**。
        return QSize(472, 214)

    def paintEvent(self, _event):                              # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        paint_arm(painter, QRectF(0, 0, self.width(), self.height()),
                  self.trunk, self.targets, self.currents)
        painter.end()


def axis_span_hint(group: str) -> str:
    """给对话框用的一句"这一组有多少轴、量程从哪到哪"。"""
    from ..core_motion import TRUNK_AXES
    axes = TRUNK_AXES if group == "trunk" else ARM_AXES[group]
    return "、".join(f"{key} {low:g}~{high:g}" for key, low, high, _hint in axes)
