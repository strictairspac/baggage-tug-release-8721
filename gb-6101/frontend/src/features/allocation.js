import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, section } from "../components/common/panel.js";
import { table } from "../components/common/table.js";
import { submit } from "../hooks/submit.js";
import { el } from "../utils/dom.js";

function today() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function minuteToInput(minute) {
  return `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}

function statusLabel(status) {
  return { active: "有效", cancelled: "已取消" }[status] || status;
}

function allocationTable(items, casesById, operator, reload) {
  if (!items.length) return empty("当天还没有领用记录。");
  return table(
    ["单号", "班组", "交接批次", "日期", "用车时段", "数量", "牵引车", "状态", "取消"],
    items.map(allocation => {
      const cancelButton = allocation.status === "active" && operator
        ? el("button", {
          type: "button",
          class: "quiet",
          "data-testid": `cancel-allocation-${allocation.id}`,
          onclick: async event => {
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = "取消中…";
            try {
              await api.cancelAllocation(allocation.id);
              await reload();
            } catch (error) {
              button.disabled = false;
              button.textContent = "重试取消";
              button.title = error.message;
              button.setAttribute("aria-label", `重试取消：${error.message}`);
            }
          },
        }, "取消")
        : el("span", { "data-testid": `allocation-status-${allocation.id}` }, statusLabel(allocation.status));
      return [
        `#${allocation.id}`,
        allocation.team_name || allocation.team || allocation.actor,
        casesById.get(allocation.case_id)?.title || (allocation.case_id ? `批次 #${allocation.case_id}` : "临时资料"),
        allocation.service_day || allocation.date,
        `${allocation.start_time || minuteToInput(allocation.start_minute)}–${allocation.end_time || minuteToInput(allocation.end_minute)}`,
        allocation.quantity,
        el("ul", { class: "asset-list", "data-testid": `allocation-assets-${allocation.id}` },
          allocation.assets.map(asset => el("li", {}, asset))),
        el("span", { class: `allocation-state state-${allocation.status}`, "data-testid": `allocation-state-${allocation.id}` },
          statusLabel(allocation.status)),
        cancelButton,
      ];
    }),
    "牵引车领用记录",
  );
}

export function allocationPanel({ cases = [], selectedCaseId = null, user, signal } = {}) {
  const operator = user?.role === "operator";
  const casesById = new Map(cases.map(item => [item.id, item]));
  const preferredCase = cases.find(item => item.id === selectedCaseId)
    || cases.find(item => item.title.includes("领车"))
    || cases[0];

  const success = el("p", { class: "feedback allocation-success", role: "status", "data-testid": "allocation-success" });
  const listHost = el("div", { "data-testid": "allocation-list" }, empty("正在加载领用记录…"));

  async function reload() {
    try {
      const items = await api.allocations({ limit: 100 });
      if (signal?.aborted) return;
      listHost.replaceChildren(allocationTable(items, casesById, operator, reload));
    } catch (error) {
      if (!signal?.aborted) listHost.replaceChildren(el("p", { role: "alert" }, error.message));
    }
  }

  const form = el("form", { class: "allocation-form", "data-testid": "allocation-form" },
    field("交接批次", el("select", { name: "case_id", required: true },
      cases.length ? cases.map(item => el("option", {
        value: item.id,
        selected: preferredCase?.id === item.id,
      }, item.title)) : el("option", { value: "" }, "暂无批次"))),
    field("班组", el("input", {
      name: "team_name", maxlength: 120, placeholder: "例如：甲班", value: user?.username ? `${user.username}班` : "",
      autocomplete: "organization",
    }), "两个班组可分别填写，车号由全局时段锁统一分配。"),
    field("日期", el("input", { name: "service_date", type: "date", required: true, value: today() })),
    field("开始时间", el("input", {
      name: "start_time", type: "time", required: true, value: "08:00", "data-testid": "allocation-start-time",
    }), "用车开始时刻；半开时段，开始分钟占用车辆。"),
    field("开始分钟", el("input", {
      name: "start_minute", type: "number", min: 0, max: 1439, step: 1, value: 480, required: true,
      "data-testid": "allocation-start",
    }), "与时钟时间同步；480 即 08:00。"),
    field("结束时间", el("input", {
      name: "end_time", type: "time", required: true, value: "09:00", "data-testid": "allocation-end-time",
    }), "用车结束时刻；与下一段开始时刻相同不算重叠。"),
    field("结束分钟", el("input", {
      name: "end_minute", type: "number", min: 1, max: 1440, step: 1, value: 540, required: true,
      "data-testid": "allocation-end",
    }), "540 即 09:00，1440 为 24:00。"),
    field("数量", el("input", {
      type: "number", name: "quantity", min: 1, max: 400, step: 1, value: 1, required: true,
      "data-testid": "allocation-quantity",
    }), "需要领用的牵引车数量。"),
    el("div", { class: "allocation-actions" },
      operator ? el("button", { type: "submit", "data-testid": "submit-allocation" }, "领车") : null,
      errorText(),
      success),
  );

  const startTimeInput = form.elements.start_time;
  const startMinuteInput = form.elements.start_minute;
  const endTimeInput = form.elements.end_time;
  const endMinuteInput = form.elements.end_minute;
  startTimeInput?.addEventListener("change", () => {
    const [hour, minute] = startTimeInput.value.split(":").map(Number);
    if (Number.isInteger(hour) && Number.isInteger(minute)) startMinuteInput.value = hour * 60 + minute;
  });
  startMinuteInput?.addEventListener("input", () => {
    const minute = Number(startMinuteInput.value);
    if (minute >= 0 && minute < 1440) startTimeInput.value = minuteToInput(minute);
  });
  endTimeInput?.addEventListener("change", () => {
    const [hour, minute] = endTimeInput.value.split(":").map(Number);
    if (Number.isInteger(hour) && Number.isInteger(minute)) endMinuteInput.value = hour * 60 + minute;
  });
  endMinuteInput?.addEventListener("input", () => {
    const minute = Number(endMinuteInput.value);
    if (minute >= 0 && minute < 1440) endTimeInput.value = minuteToInput(minute);
  });

  if (operator) {
    submit(form, async values => {
      success.textContent = "";
      const startMinute = Number(values.get("start_minute"));
      const endMinute = Number(values.get("end_minute"));
      const payload = {
        case_id: Number(values.get("case_id")),
        team_name: values.get("team_name").trim(),
        service_date: values.get("service_date"),
        start: startMinute,
        end: endMinute,
        start_minute: startMinute,
        end_minute: endMinute,
        start_time: minuteToInput(startMinute),
        end_time: minuteToInput(endMinute),
        quantity: Number(values.get("quantity")),
      };
      const saved = await api.createAllocation(payload);
      success.textContent = `领用成功：${saved.assets.join("、")}。并发撞车时已自动从仍空闲的合格车补齐；不足数量时不会占车。`;
      await reload();
    });
  }

  void reload();
  if (signal) {
    const timer = window.setInterval(() => void reload(), 1200);
    signal.addEventListener("abort", () => window.clearInterval(timer), { once: true });
  }
  return section("牵引车领用 / 放行派车",
    el("p", { class: "allocation-rule" },
      "请按当天的用车起止时间和数量领车。仅分配整个用车时段内全部必检均有效的牵引车；同一车的半开使用时段不能重叠，首尾相接可以。并发撞车时自动从其余空闲合格车补齐，不足则整单不占车。"),
    operator ? form : el("p", { class: "empty" }, "当前账号可查看领用安排；领车和取消需使用 operator 账号。"),
    success,
    listHost);
}
