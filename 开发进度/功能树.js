"use strict";

// Group names match the development ledger. Keep capabilities independent of
// milestones: a completed research task does not turn a backtest into a live feature.
const featureFamilies = [
  {name:"运行模式", modules:[
    {id:"quant-terminal", group:"量化交易台", hint:"策略、仓位、成交和盘中信号", description:"本地交易台可按交易日回放策略，并读取盘中 Agent 的有效信号。uSMART 实盘账户等待读取权限及联调。", features:[
      ["账户和成交回放","选择标的和策略，逐日查看规则目标、模拟仓位、账户净值、回撤及成交明细。"],
      ["盘中分析与决策入口","查看同一轮盘中信号、卖出风险和仓位验证。自动读取可在超时后重试，也可手动导入报告。过期观察标记为存档，历史统计缺失时仍可查看本轮行情。"]
    ]},
    {id:"regular", group:"常规分析", hint:"多维研究与最终报告", description:"按标的和分析日期生成研究报告，汇总各方观点，并用行情和仓位校验约束最终摘要。", features:[
      ["四类分析师","可选择行情技术、基本面、新闻和市场情绪，分别收集研究依据。"],
      ["多空研究与交易方案","多空双方辩论后，研究经理形成计划，再由交易员提出方案。"],
      ["风险讨论与组合结论","不同风险立场展开讨论，组合经理汇总观点，仓位规则随后独立校验。"],
      ["报告与复盘","终端、Markdown 和 HTML 展示核验后的结论。后续分析可参考此前决策及已复盘的结果。"]
    ]},
    {id:"intraday", group:"盘中分析与决策", hint:"盘中信号、卖出风险与仓位验证", description:"在美股常规交易时段，根据完整分钟线解释当前观察。TQQQ 同时参考 QQQ。卖出风险和仓位验证共同归在这项决策下。", features:[
      ["交易时段与分钟线","分钟趋势只用两标的共同完成且仍有效的分钟线，快的一方回切并重算指标；无法对齐时等待。"],
      ["09:35 VXN 分批减仓","根据前日 VXN 和盘前方向计算目标仓位，每日保留首个有效结果。"],
      ["前向模拟减仓账本","查看提示后的模拟成交、15 分钟和收盘时的净差及费用。"],
      ["逐项技术分析","分别列出价格偏离 VWAP 的幅度、EMA9 相对 EMA20 的差幅及五分钟动量。方向不一致时，完整报告给出偏强和偏弱分别还缺哪些条件。"],
      ["与上一轮对比","对比价格、动量、因子方向及观察结果，区分新分钟、同一分钟修订和重复刷新。行情时间回退时暂停判断。"],
      ["模型复核与等待","新信号可交给模型复核。方向冲突或数据过期时显示等待。"],
      ["卖出风险","风险观察获确认后，对照盘前背景和常规盘走势。载入本地校准数据后，TQQQ 可查看类似触发后下跌时少亏、反弹时少赚、扣费后净差及卖出占优比例。"],
      ["仓位验证","09:35 模式使用带时区的账户快照和可卖数量，校验方向、费用、仓位及风险参数后展示股数。只有日期的快照仍提示待补。"],
      ["盘前辅助卖出判断","常规盘出现 TQQQ 与 QQQ 双弱时，补充当天美东 04:00–09:30 已结束的同步行情。盘前数据不足时沿用常规盘依据，跨午夜夜盘待数据接入。"]
    ]}
  ]},
  {name:"共享能力", modules:[
    {id:"data", group:"数据获取", hint:"行情、新闻与宏观日历", description:"为研究提供带日期的资料，并保留能判断数据是否可用的来源和日期信息。", features:[
      ["日线与指标","核对实际行情日期、最近完整交易日及技术指标。"],
      ["新闻与宏观预期","汇集新闻、经济事件和可获取的共识预期，供分析师与公布值对照。"],
      ["休市与断源回退","读取符合条件的历史缓存，明确标记缺失或已过期的数据。"]
    ]},
    {id:"broker", group:"券商接入", hint:"uSMART 账户只读接入", description:"计划读取本人的账户数据用于对账和研究。正式权限与接入进度见下方任务。", features:[
      ["申请与测试环境","整理 OpenAPI 申请和测试环境验证材料。正式授权是账户联调的前提。"],
      ["Agent UAT 查询测试","独立入口连接公共 UAT 账号，查询行情、测试持仓和分钟线，交给现有盘中校验与判断引擎。旧数据或缺口会等待；不发单、不调用付费模型，正式账户仍待授权。"],
      ["持仓与盈亏对账","接入目标为同一时点核对持仓、币种和盈亏口径。"]
    ]}
  ]},
  {name:"研究与维护", modules:[
    {id:"research", group:"策略研究", hint:"合成策略演示", description:"公开源码用可复现的合成分钟行情演示 TQQQ 和 QQQ 的仓位、交易费用和净值变化。", features:[
      ["四组规则与持有基准","比较双弱卖出双强买回、早盘与突破各半、早盘分批减仓及尾盘减半。"],
      ["合成回放","演示数据由仓库中的生成程序产生。两个标的各自独立记账。"]
    ]},
    {id:"tools", group:"开发工具", hint:"任务、历史与验收证据", description:"本地开发面板将功能结构、开发任务和验收证据放在同一个入口。", features:[
      ["功能树与任务看板","按模块浏览能力，或按开发状态筛选任务。"],
      ["项目动态与证据","沿时间查看开发记录，打开对应问题、处理办法和验收文件。"]
    ]}
  ]}
];

function treeFamilies(tasks) {
  const groups = new Set(featureFamilies.flatMap(f => f.modules.map(m => m.group)));
  const newGroups = [...new Set(tasks.map(t => t.group).filter(g => g && !groups.has(g)))];
  if (!newGroups.length) return featureFamilies;
  const modules = newGroups.sort((a,b) => a.localeCompare(b,"zh")).map(group => ({
    id:"group-" + group, group, hint:"已登记开发任务", description:"此模块来自项目开发记录。", features:[]
  }));
  return [...featureFamilies, {name:"新登记模块", modules}];
}

function taskCountLabel(items) {
  if (!items.length) return "";
  const state = ["受阻","开发中","待验证","待开发","已完成"].find(s => items.some(t => t.state === s));
  return state ? items.filter(t => t.state === state).length + " 项" + state : "";
}

function renderFeatureTree() {
  const families = treeFamilies(tasks);
  const modules = families.flatMap(f => f.modules);
  const active = modules.find(m => m.id === activeModule) || modules[0];
  const focusKey = document.activeElement?.dataset.focusKey;
  const branches = $("branches");
  branches.replaceChildren();
  branches.style.setProperty("--families",families.length);
  $("treeSize").textContent = families.length + " 条主线 · " + modules.length + " 个模块";
  for (const family of families) {
    const section = el("section",undefined,"family");
    section.append(el("h3",family.name,"family-name"));
    const list = el("ul",undefined,"module-list");
    for (const mod of family.modules) {
      const items = tasks.filter(t => t.group === mod.group);
      const li = el("li"), button = el("button",undefined,"module-node");
      button.type = "button";
      button.dataset.module = mod.id;
      button.dataset.focusKey = "m-" + mod.id;
      button.setAttribute("aria-pressed",String(mod.id === active.id));
      button.setAttribute("aria-controls","moduleDetail");
      const title = el("span",undefined,"module-title");
      title.append(el("strong",mod.group),el("span","›","node-arrow"));
      button.append(title,el("span",mod.hint,"module-hint"));
      const label = taskCountLabel(items);
      if (label) {
        const status = el("span",label,"node-status");
        status.dataset.status = items.find(t => label.endsWith(t.state)).state;
        button.append(status);
      }
      button.onclick = () => {
        activeModule = mod.id;
        setLocation("tree/"+encodeURIComponent(mod.id));
        renderFeatureTree();
        if (matchMedia("(max-width:980px)").matches) {
          $("moduleHeading").focus({preventScroll:true});
          $("moduleDetail").scrollIntoView({block:"start"});
        }
      };
      li.append(button); list.append(li);
    }
    section.append(list); branches.append(section);
  }
  const detail = $("moduleDetail");
  detail.replaceChildren();
  const family = families.find(f => f.modules.includes(active));
  const heading = el("h2",active.group);
  heading.id = "moduleHeading"; heading.tabIndex = -1;
  const back = el("button","↑ 返回分支","tree-back");
  back.type = "button";
  back.onclick = () => {
    const button = branches.querySelector('[aria-pressed="true"]');
    button.focus({preventScroll:true}); button.scrollIntoView({block:"center"});
  };
  const trail = el("div",undefined,"module-trail");
  trail.append(el("span","股票 Agent / " + family.name),back);
  detail.append(trail,heading,el("p",active.description,"module-intro"));
  if (active.features.length) {
    detail.append(el("h3","分支功能","section"));
    const list = el("ul",undefined,"capabilities");
    for (const [title,description] of active.features) {
      const li = el("li");
      li.append(el("strong",title),el("p",description)); list.append(li);
    }
    detail.append(list);
  }
  detail.append(el("h3","关联开发记录","section"));
  const items = tasks.filter(t => t.group === active.group);
  if (!items.length) detail.append(el("p",revision < 0 ? "正在读取开发记录…" : "该模块暂无开发记录。","meta"));
  for (const t of items) {
    const b = el("button",undefined,"linked-task"); b.type = "button";
    b.dataset.focusKey = "task-" + t.id;
    b.append(tag(t.state),el("span",t.title),el("span","↗","task-arrow"));
    b.onclick = () => {view="board";filter="全部";choose(t.id,true);};
    detail.append(b);
  }
  if (focusKey) {
    const matching = [...$("featureTree").querySelectorAll("[data-focus-key]")].find(b => b.dataset.focusKey === focusKey);
    matching?.focus({preventScroll:true});
  }
}
