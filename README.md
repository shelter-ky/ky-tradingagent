# KY 股票 Agent 0.0.1

面向美股和 ETF 的本地研究工具。启动时可选择常规研究报告或盘中分钟分析。量化交易台集中展示策略回放、卖出风险和仓位验证。

## 打开交易台

Windows 用户可双击 `量化交易台/启动交易台.cmd`。其他系统在项目根目录运行以下命令，需要 Python 3.11 或更高版本。

```sh
python 量化交易台/serve.py
```

服务只监听本机地址。双击 `量化交易台/交易面板.html` 也可查看回放，并手动导入盘中 Agent 保存的 `latest.json`。

公开版默认载入 **20 个虚构交易日的合成演示**，用于体验 TQQQ、QQQ 的策略切换、逐日播放和成交明细。历史研究的汇总收益、回撤、成交成本与样本限制见[四组合评估](outputs/四组合双标的评估/四组合评估.html)。已有本地完整版的用户可以复制自己的 `量化交易台/回放数据.js` 覆盖演示文件。

## 运行股票分析

在独立 Python 环境安装依赖后，从项目根目录启动。Windows 也可双击 `启动股票Agent.cmd` 自动建立环境。

```sh
python -m pip install -e .
python -m cli.main --ticker TQQQ
```

- **常规研究**可选择技术、基本面、新闻和情绪分析，生成包含资料时间、分歧和风险条件的报告。
- **盘中分析与决策**检查分钟线时效。分析 TQQQ 时参考 QQQ。减仓观察可查看同类历史事件中卖出后少亏和少赚的金额。仓位验证会列出资料缺口，仅有日期的账户快照不能验证盘中数量。
- **uSMART** 提供独立的公共 UAT 只读查询入口。可运行 `python -m tradingagents.uat_check --help` 查看所需的官方测试材料。正式账户页面待接入。本版本不向券商下单。

首次运行按终端提示选择模型和提供方。自行配置模型所需的登录或密钥，行情和模型调用取决于所选服务。开发面板的入口是 `开发进度/股票Agent开发进度.html`。

## 测试与研究

```sh
python -m pip install -e ".[dev,research]"
python -m pytest tests
python work/build_demo_replay.py
```

`work/` 和根目录的研究脚本包含策略、成交假设和复核逻辑。完整历史重跑需要自行准备对应脚本所列的分钟行情、日线、波动率及分红档案。模拟成交和历史收益不代表真实账户成交。

上游代码来自 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents/tree/9fa1196fffbcfd9de5146d2dd2869df84ab99838)。变更与许可见 [NOTICE](NOTICE) 和 [LICENSE](LICENSE)。
