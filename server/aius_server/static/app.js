"use strict";
// AI 运营洞察平台的页面脚本。主题、图标、提示条、二次确认在 ui-kit.js（UIKit，与运维控制台共用）。
// 页面是服务端渲染的：这里只做增强（复制、确认、筛选、联想、提问、同步进度、画图）。
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  // ---- 操作结果提示：服务端跳转回来时带 ?done=xxx，渲染在 #flash 里 ----
  const flash = $("#flash");
  if (flash) {
    UIKit.notify(flash.textContent, { type: flash.dataset.type });
    const url = new URL(location.href);
    if (url.searchParams.has("done")) {
      url.searchParams.delete("done"); // 刷新或收藏时不再重复提示
      history.replaceState(null, "", url);
    }
  }

  // ---- 复制 ----
  // 平台常常跑在内网 http 上，不是安全上下文，navigator.clipboard 不可用，所以要有 execCommand 兜底
  async function copyText(text) {
    if (window.isSecureContext && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {}
    }
    const box = document.createElement("textarea");
    box.value = text;
    box.readOnly = true;
    box.className = "offscreen";
    (document.querySelector("dialog:modal") || document.body).append(box);
    box.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {}
    box.remove();
    return ok;
  }

  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy-from]");
    if (copy) {
      const source = document.getElementById(copy.dataset.copyFrom);
      const ok = await copyText(source.value ?? source.textContent);
      UIKit.notify(ok ? "已复制" : "复制失败，请手动选中后复制", { type: ok ? "success" : "error" });
      return;
    }
    const opener = event.target.closest("[data-open-dialog]");
    if (opener) return void document.getElementById(opener.dataset.openDialog)?.showModal();
    const closer = event.target.closest("[data-close-dialog]");
    if (closer) return void closer.closest("dialog")?.close();
    // 「填名称 / 改名」：把这一行的应用 ID 和现有名称填到上面的表单里
    const fill = event.target.closest("[data-fill]");
    if (fill) {
      document.getElementById(fill.dataset.fill).value = fill.dataset.fillValue;
      const also = document.getElementById(fill.dataset.fillAlso);
      also.value = fill.dataset.fillAlsoValue;
      also.focus();
      also.scrollIntoView({ block: "center" });
      return;
    }
    // 用 GET 重新打开当前地址，而不是 reload()：当前页如果是表单提交的结果，reload 会重复提交
    if (event.target.closest("#reload")) location.assign(location.href.split("#")[0]);
  });
  for (const dialog of $$("dialog[data-open]")) dialog.showModal(); // 「修改」带着已有内容直接打开对话框

  // ---- 表单：危险操作先二次确认；提交后禁用按钮，防止重复提交 ----
  const lock = (form) => {
    for (const button of $$("button[type=submit]", form)) button.disabled = true;
  };
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (form.id === "ask-form" || form.method === "dialog" || form.method === "get") return;
    if (form.dataset.confirm && !form.dataset.confirmed) {
      event.preventDefault();
      const ok = await UIKit.confirm({
        title: form.dataset.confirm,
        message: form.dataset.confirmMessage || "",
        confirmText: form.dataset.confirmText || "确认",
        danger: "danger" in form.dataset,
      });
      if (!ok) return;
      form.dataset.confirmed = "1";
      lock(form);
      form.submit();
      return;
    }
    setTimeout(() => lock(form)); // 等表单数据收集完再禁用
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) for (const button of $$("button[type=submit][disabled]")) button.disabled = false;
  });

  // ---- 选了就打开：「按月」下拉框（没有脚本时有一个「查看」按钮） ----
  for (const select of $$("select[data-autosubmit]")) {
    select.addEventListener("change", () => {
      if (select.value) select.form.requestSubmit();
    });
  }

  // ---- 表格筛选：只在当前页面已有的行里筛 ----
  for (const input of $$("[data-filter-input]")) {
    const table = document.getElementById(input.dataset.filterInput);
    const rows = $$("tbody tr:not([data-empty])", table);
    const empty = $("tr[data-empty]", table);
    input.addEventListener("input", () => {
      const query = input.value.trim().toLowerCase();
      let shown = 0;
      for (const row of rows) {
        row.hidden = !!query && !row.textContent.toLowerCase().includes(query);
        if (!row.hidden) shown++;
      }
      if (empty) empty.hidden = shown > 0;
    });
  }

  // ---- 输入联想：部门、人员、应用。列表里选中的是 ID，旁边显示名称 ----
  for (const input of $$("input[data-suggest]")) {
    const list = document.getElementById(input.getAttribute("list"));
    let timer = 0;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const query = input.value.trim();
      if (!query) return void list.replaceChildren();
      timer = setTimeout(async () => {
        try {
          const response = await fetch(`/suggest?kind=${input.dataset.suggest}&q=${encodeURIComponent(query)}`);
          if (!response.ok) return;
          const options = ((await response.json()).items || []).map((item) => {
            const option = document.createElement("option");
            option.value = item.value;
            option.label = item.label;
            option.textContent = item.label;
            return option;
          });
          list.replaceChildren(...options);
        } catch {} // 联想失败不影响手工输入
      }, 200);
    });
  }
  // 额度表单：对象类型换了，联想的范围跟着换
  for (const select of $$("select[data-suggest-kind]")) {
    const input = document.getElementById(select.dataset.suggestKind);
    const kinds = { department: "dept", member: "user", app: "app" };
    const apply = (clear) => {
      input.dataset.suggest = kinds[select.value] || "dept";
      input.placeholder = select.value === "app" ? "应用 ID，或输入名称从列表里选" : "输入名称，从列表里选";
      if (clear) input.value = "";
    };
    select.addEventListener("change", () => apply(true));
    apply(false);
  }

  // ---- AI 门户：提问 ----
  const ask = $("#ask-form");
  if (ask) {
    const input = $("#ask-input");
    const submit = $("#ask-submit");
    const panel = $("#answer-panel");
    const answer = $("#answer");
    const run = async (question) => {
      panel.hidden = false;
      $("#answer-question").textContent = question;
      $("#answer-meta").textContent = "正在取数并分析…";
      const loading = document.createElement("div");
      loading.className = "loading-panel";
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      loading.append(spinner, "正在分析，通常需要十几秒");
      answer.replaceChildren(loading);
      submit.disabled = true;
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      try {
        const response = await fetch(ask.dataset.url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `提问失败（HTTP ${response.status}），请稍后再试`);
        // data.html 是服务端把回答里的 Markdown 转成的 HTML，文字都已转义
        answer.innerHTML = data.html;
        $("#answer-meta").textContent = `数据范围：${data.scope} · ${data.range}${data.model ? "" : " · 没有经过 AI 模型，按数据直接整理"}`;
      } catch (problem) {
        const box = document.createElement("p");
        box.className = "error";
        box.setAttribute("role", "alert");
        box.textContent = problem.message || "提问失败，请检查网络后重试";
        answer.replaceChildren(box);
        $("#answer-meta").textContent = "";
      } finally {
        submit.disabled = false;
      }
    };
    ask.addEventListener("submit", (event) => {
      event.preventDefault();
      const question = input.value.trim();
      if (question) run(question);
    });
    for (const chip of $$("[data-ask]"))
      chip.addEventListener("click", () => {
        input.value = chip.dataset.ask;
        run(chip.dataset.ask);
      });
  }

  // ---- 同步管理：任务进度，每 3 秒问一次；任务结束后重新打开页面看结果 ----
  const progress = $("#sync-progress");
  if (progress) {
    const number = (n) => Number(n).toLocaleString("en-US");
    const poll = async () => {
      try {
        const response = await fetch(progress.dataset.statusUrl);
        if (response.ok) {
          const { active } = await response.json();
          if (!active) return void location.assign("/sync");
          $("#sync-bar").max = Math.max(active.units, 1);
          $("#sync-bar").value = active.done;
          // 进度的说法由服务端拼好（和页面第一次打开时显示的一样）；旧版本服务端没有 text 时自己拼一句
          $("#sync-text").textContent =
            active.text ||
            `${active.status_label}：已完成 ${active.done} / ${active.units} 个数据集，写入 ${number(active.rows)} 条` +
              (active.failed ? `，${active.failed} 个失败` : "") +
              (active.stop_requested ? "，正在停止…" : "");
        }
      } catch {} // 一次没问到没关系，下次再问
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  }

  // ---- 图表 ----
  // 都是单一序列：柱（每天 / 每小时的用量）或线（人数、人均）。柱最宽 24px、顶部 4px 圆角、从同一条基线长出来；
  // 线 2px，末端一个带描边的点。只标最高值（柱）或末端值（线），其余靠坐标轴、悬停提示和图下面的「看数字」表格。
  const SVG_NS = "http://www.w3.org/2000/svg";
  const svgNode = (tag, attributes = {}, text) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const trim = (n, digits) => Number(n.toFixed(digits)).toLocaleString("en-US");
  // 坐标轴：一万以内写全，往上写成「1.5万」
  const tickText = (value, top) => (top >= 10000 ? `${trim(value / 10000, 1)}万` : trim(value, 1));
  const valueText = (value) => trim(value, 2);

  // 纵轴：刻度间隔取 1/2/5×10^n，最多 5 条线
  function axis(max) {
    const rough = Math.max(max, 1) / 4;
    const power = 10 ** Math.floor(Math.log10(rough));
    const step = Math.max(1, [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough));
    return { step, top: Math.ceil(Math.max(max, 1) / step) * step };
  }

  function draw(box) {
    const spec = JSON.parse(box.dataset.chart);
    const points = spec.points;
    const width = box.clientWidth;
    const height = box.clientHeight;
    if (!width || !points.length) return;
    const pad = { left: 46, right: 14, top: 20, bottom: 22 };
    const max = Math.max(...points.map((p) => p.value));
    const { step, top } = axis(max);
    const plotHeight = height - pad.top - pad.bottom;
    const base = pad.top + plotHeight;
    const band = (width - pad.left - pad.right) / points.length;
    const center = (index) => pad.left + band * index + band / 2;
    const y = (value) => base - (value / top) * plotHeight;
    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });

    for (let value = 0; value <= top; value += step) {
      const line = Math.round(y(value)) + 0.5;
      svg.append(svgNode("line", { class: "grid", x1: pad.left, x2: width - pad.right, y1: line, y2: line }));
      svg.append(svgNode("text", { class: "tick", x: pad.left - 8, y: line + 4, "text-anchor": "end" }, tickText(value, top)));
    }
    const every = Math.ceil(46 / band); // 横轴文字隔几个标一个，保证不重叠；最后一个一定标
    points.forEach((point, index) => {
      if ((points.length - 1 - index) % every === 0)
        svg.append(svgNode("text", { class: "tick", x: center(index), y: height - 6, "text-anchor": "middle" }, point.label));
    });

    const marks = []; // 每个点对应的可高亮元素（柱）
    const cross = svgNode("line", { class: "cross", y1: pad.top, y2: base, visibility: "hidden" });
    const focusDot = svgNode("circle", { class: "dot", r: 4, visibility: "hidden" });
    if (spec.kind === "line") {
      const path = points.map((p, i) => `${i ? "L" : "M"}${center(i)},${y(p.value)}`).join("");
      if (points.length > 1) {
        svg.append(svgNode("path", { class: "area", d: `${path}L${center(points.length - 1)},${base}L${center(0)},${base}Z` }));
        svg.append(svgNode("path", { class: "line", d: path }));
      }
      const last = points.length - 1;
      svg.append(svgNode("circle", { class: "dot", cx: center(last), cy: y(points[last].value), r: 4 }));
      svg.append(
        svgNode("text", { class: "peak", x: center(last), y: y(points[last].value) - 10, "text-anchor": "end" }, valueText(points[last].value)),
      );
      svg.append(cross, focusDot);
    } else {
      const barWidth = Math.max(2, Math.min(24, band - 2)); // 相邻的柱之间至少留 2px
      const peak = points.findIndex((p) => p.value === max);
      points.forEach((point, index) => {
        if (point.value <= 0) return void marks.push(null);
        const left = center(index) - barWidth / 2;
        const barTop = y(point.value);
        const radius = Math.min(4, barWidth / 2, base - barTop);
        const bar = svgNode("path", {
          class: "bar",
          d:
            `M${left},${base}V${barTop + radius}a${radius},${radius} 0 0 1 ${radius},${-radius}` +
            `h${barWidth - 2 * radius}a${radius},${radius} 0 0 1 ${radius},${radius}V${base}Z`,
        });
        svg.append(bar);
        marks.push(bar);
        if (index === peak)
          svg.append(svgNode("text", { class: "peak", x: center(index), y: barTop - 6, "text-anchor": "middle" }, valueText(point.value)));
      });
    }

    let active = -1;
    const hide = () => {
      $(".chart-tip", box)?.remove();
      marks[active]?.classList.remove("on");
      cross.setAttribute("visibility", "hidden");
      focusDot.setAttribute("visibility", "hidden");
      active = -1;
    };
    const show = (index) => {
      hide();
      active = index;
      const point = points[index];
      marks[index]?.classList.add("on");
      if (spec.kind === "line") {
        cross.setAttribute("x1", center(index));
        cross.setAttribute("x2", center(index));
        cross.setAttribute("visibility", "visible");
        focusDot.setAttribute("cx", center(index));
        focusDot.setAttribute("cy", y(point.value));
        focusDot.setAttribute("visibility", "visible");
      }
      // 提示里数值在前、说明在后；文字一律用 textContent 写入
      const tip = document.createElement("div");
      tip.className = "chart-tip";
      const value = document.createElement("strong");
      value.textContent = `${valueText(point.value)} ${spec.unit}`;
      tip.append(value);
      for (const line of [point.title, ...(point.lines || [])]) tip.append(document.createElement("br"), line);
      box.append(tip);
      const half = tip.offsetWidth / 2; // 靠边的点，提示框贴着图的边，不伸出去
      tip.style.left = `${Math.min(Math.max(center(index), half), box.clientWidth - half)}px`;
      tip.style.top = `${y(point.value)}px`;
    };
    // 悬停区域是整条竖带，比标记本身大得多，0 值的点也能指到
    points.forEach((_point, index) => {
      const hit = svgNode("rect", { class: "hit", x: pad.left + band * index, y: pad.top, width: band, height: plotHeight });
      hit.addEventListener("pointerenter", () => show(index));
      hit.addEventListener("pointerleave", hide);
      svg.append(hit);
    });
    box.replaceChildren(svg);
    // 键盘：聚焦到图上之后用左右方向键逐个看，Esc 收起
    box.onkeydown = (event) => {
      if (event.key === "Escape") return void hide();
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const next = active < 0 ? points.length - 1 : active + (event.key === "ArrowRight" ? 1 : -1);
      show(Math.min(points.length - 1, Math.max(0, next)));
    };
    box.onblur = hide;
  }

  for (const box of $$(".chart[data-chart]")) new ResizeObserver(() => draw(box)).observe(box);
})();
