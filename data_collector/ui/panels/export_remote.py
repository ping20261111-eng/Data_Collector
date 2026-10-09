"""远端凭据：问一次密码 + 探一次连接（导出目标里"服务器"那个点由此而来）。

从 `export_target.py` 拆出来（v6.4.9）。原因与 v6.4.5 拆它那次一样：
补上这一段后 `export_target.py` 到了 **1026 行**，顶过了"单个界面文件不超过
1000 行"的上限守卫。**拆出来而不是调高上限** —— 那条守卫的存在就是为了拦住
这种膨胀，放宽它等于把尺子改短来显得自己没长高。

为什么做成 `Mixin` 而不是独立控件类：这一段与 `ExportTargetMixin` 共享同一批
界面对象（`self.append` 日志、`self.uploader` 凭据、`self.chip_glyph` 芯片），
拆成独立类就要把十几个引用逐个透传，收益不抵改动量。
Mixin 只是把"同一批代码"换个文件放，`MainWindow` 那边一行都不用改。
"""

from ..common import ensure_remote_password
from ..threads import LocalTaskThread


class RemoteCredentialMixin:
    """服务器目标的状态：凭据就绪 → 绿点，探连失败 → 琥珀点 + 「未就绪」。

    `disk` 目标永远不会走到这里（要不要问远端密码由 `REMOTE_TARGETS` 决定）：
    离线现场根本没人知道服务器密码，为了写本地盘被问一次密码是纯粹的骚扰。
    """

    def _init_remote_state(self) -> None:
        """只放内存的状态。

        它描述的是"本次运行试过没有、成没成"，跨重启没有意义 ——
        密码本身也只在内存里（`ensure_remote_password` 的约定）。
        """
        self._remote_fail = ""            # 非空 = 探连失败过（值是一句短原因，供日志）
        self._remote_probe_thread = None  # 探连线程的引用：不挂父对象，见 threads.py

    def _remote_ready(self) -> bool:
        """本次运行是否已有服务器凭据。

        读 `uploader.password` 而不是自己记一个标记：密码只存在内存里，
        **任何入口（导出面板 / 上传 / 浏览远端目录）输入的都是同一个字段** ——
        芯片因此不会出现"我明明输过密码，它还是灰的"这种自相矛盾。
        """
        return bool(getattr(getattr(self, "uploader", None), "password", ""))

    def _remote_chip_state(self):
        """服务器目标下芯片该显示什么：`(状态名, 文案)`。

        - 本次运行还没拿到密码 → 灰点、无文案（"还不知道能不能用"）
        - 凭据就绪 → 绿点（可以直接导出到服务器）
        - 凭据就绪但探连失败 → 琥珀点 + 「未就绪」（现在传不了，原因在日志区）

        ## 为什么灰/绿看"有没有密码"，而不看"探连过没有"

        用户输完密码那一刻就该有反馈，而探连要走网络（可能几秒）。先按
        "凭据已就绪"变绿，探连结果回来再纠正 —— 比"输完密码还是灰点、
        要等两三秒才知道成了没有"清楚得多。**探连回答的是"现在能不能用"，
        它只负责把绿点推翻成琥珀**（认证失败 / 连不上），不负责把灰点提前点亮。
        """
        if getattr(self, "_remote_fail", ""):
            return "warn", "未就绪"
        if self._remote_ready():
            return "ok", ""
        return "none", ""

    def _prompt_remote_credentials(self) -> None:
        """问一次远端密码，然后在后台验一次连接（v6.4.9）。

        ## 为什么把"问密码"提前到选目标这一刻

        以前密码只在真正要传的时候才问（上传 / 浏览远端目录），于是从"选了服务器"
        到"第一次真的传"之间，界面没有任何办法说明"这台机器到底连不连得上"：
        芯片一直是灰点，现场看不出区别，直到导出那一刻才发现连不上 —— 而那时
        数据已经在等着了。现在选「服务器」/「两者」就当场问、当场验，结论直接
        落在芯片上（绿点 = 凭据就绪；琥珀 + 「未就绪」 = 现在传不了）。

        ## 只在没有凭据时才弹框

        密码已经在内存里就不问第二遍 —— 问第二遍是骚扰，现场会以为程序没记住。
        **探连失败会清掉密码**（认证失败时），那时再点会重新问，这正是想要的：
        密码错了就该重输。

        ## 为什么不能只调 `ensure_remote_password` 就完事

        那个只管"拿到密码"，不管"密码能不能用"。密码打错（大小写 / 键盘布局）
        在现场很常见，而它此前只会在第一次上传时才暴露。这里多走一步探连，
        把发现错误的时刻从"传输中"提前到"刚输完"。
        """
        thread = getattr(self, "_remote_probe_thread", None)
        if thread is not None and thread.isRunning():
            return                                    # 上一次还在跑，别叠一个
        if not ensure_remote_password(self.uploader, self):
            self.append("已取消：未提供服务器密码（导出到服务器需要它）")
            self._refresh_chip()
            return
        self._start_remote_probe()

    def _start_remote_probe(self) -> None:
        """起一个后台线程探一次 SFTP 认证，结果回主线程刷芯片。

        为什么放后台：`paramiko` 建连接要握手 + 认证，网络慢时是秒级，
        放界面线程就是"点一下服务器，整个界面不动了"。
        """
        self._remote_fail = ""
        self._refresh_chip()          # 凭据刚到手：先按"就绪"刷一次，不等网络
        self.append(f"正在验证服务器连接：{self.uploader.remote_user}@"
                    f"{self.uploader.remote_host}:{self.uploader.remote_port} …")
        thread = LocalTaskThread(self._probe_remote_credentials)
        self._remote_probe_thread = thread
        thread.finished.connect(self._on_remote_probe_finished)
        thread.start()

    def _probe_remote_credentials(self) -> dict:
        """在后台线程里做一次认证探连，返回**结构化结果**（不抛异常）。

        ## 为什么不让异常穿过线程边界

        `LocalTaskThread` 会把异常压成一句 `str`，而"密码错了"与"网络不通"
        要给用户完全不同的下一步（重输密码 vs 查网线 / 找网络）。在这里就分好类，
        界面层就不用去猜那句话是什么意思 —— 那正是 `errors.py` 开头专门骂过的做法。
        """
        from ...core_data import RemoteAuthError      # 延迟导入，避免加载期耦合

        transport = sftp = None
        try:
            transport, sftp = self.uploader._connect_sftp()
        except RemoteAuthError as exc:
            return {"ok": False, "kind": "auth", "msg": str(exc)}
        except Exception as exc:                      # noqa: BLE001
            return {"ok": False, "kind": "network",
                    "msg": f"{type(exc).__name__}: {exc}"}
        finally:
            for handle in (sftp, transport):
                if handle is None:
                    continue
                try:
                    handle.close()
                except Exception:                     # noqa: BLE001
                    pass
        return {"ok": True, "kind": "ok", "msg": ""}

    def _on_remote_probe_finished(self, ok, payload, error) -> None:
        """探连结果：写日志 + 刷芯片（染色由 `_remote_chip_state` 统一决定）。"""
        result = payload if isinstance(payload, dict) else {
            "ok": False, "kind": "error", "msg": str(error or "未知错误")}
        if result.get("ok"):
            self._remote_fail = ""
            self.append("✔ 服务器连接正常（凭据已验证，本次运行内有效）")
        elif result.get("kind") == "auth":
            # 密码错就清掉 —— 与上传路径同一条规则（`upload.py` 认证失败也清密码）。
            # 不清的话，用户会带着一个错密码反复重试，而每次都只报同一句错。
            self.uploader.password = ""
            self._remote_fail = "认证失败"
            self.append("✖ 服务器认证失败：密码可能不正确（已清除，"
                        "可再点一次「服务器」重新输入）")
        elif result.get("kind") == "network":
            # 网络不通**不清密码**：密码可能是对的，只是这会儿连不上。
            self._remote_fail = "连不上"
            self.append(f"✖ 连不上服务器（{self.uploader.remote_host}:"
                        f"{self.uploader.remote_port}）：{result.get('msg', '')}")
            self.append("  凭据已保留；网络恢复后再点一次「服务器」即可重试")
        else:
            self._remote_fail = "探连失败"
            self.append(f"✖ 服务器连接验证失败：{result.get('msg', '')}")
        self._refresh_chip()
