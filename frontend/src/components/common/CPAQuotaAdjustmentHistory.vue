<script setup lang="ts">
import { computed, ref, watch } from "vue";
import PaginationControls from "./PaginationControls.vue";
import { useDateTime } from "@/composables/useDateTime";
import type { CPAQuotaAdjustment, CPAQuotaDiscrepancy } from "@/types/cpa";
import { formatCurrency } from "@/utils/formatters";

const props = defineProps<{
  adjustments: CPAQuotaAdjustment[];
  selectedCycle: Pick<
    CPAQuotaDiscrepancy,
    "cycle_started_at" | "cycle_ended_at"
  > | null;
}>();
const emit = defineEmits<{ reverse: [id: string, reason: string] }>();
const formatTime = useDateTime();
const scope = ref<"cycle" | "all">(props.selectedCycle ? "cycle" : "all");
const memberId = ref("");
const status = ref<"all" | "applied" | "reversed" | "reversal">("all");
const query = ref("");
const page = ref(1);
const pageSize = 5;
const reversingId = ref<string | null>(null);
const reverseReason = ref("");

const cycleKey = computed(() =>
  props.selectedCycle
    ? `${props.selectedCycle.cycle_started_at}/${props.selectedCycle.cycle_ended_at}`
    : "",
);
const scopedAdjustments = computed(() => {
  const cycle = props.selectedCycle;
  if (scope.value !== "cycle" || !cycle) return props.adjustments;
  return props.adjustments.filter(
    (row) =>
      Date.parse(row.cycle_started_at) === Date.parse(cycle.cycle_started_at) &&
      Date.parse(row.cycle_ended_at) === Date.parse(cycle.cycle_ended_at),
  );
});
const members = computed(() => {
  const names = new Map(
    props.adjustments.map((row) => [row.participant_id, row.participant_name]),
  );
  return [...names].map(([id, name]) => ({ id, name }));
});
const filteredAdjustments = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase();
  return scopedAdjustments.value
    .filter((row) => {
      if (memberId.value && String(row.participant_id) !== memberId.value)
        return false;
      if (status.value === "applied" && (row.reversal_of || row.reversed))
        return false;
      if (status.value === "reversed" && !row.reversed) return false;
      if (status.value === "reversal" && !row.reversal_of) return false;
      return (
        !keyword ||
        [row.participant_name, row.reason, row.created_by_name ?? ""].some(
          (value) => value.toLocaleLowerCase().includes(keyword),
        )
      );
    })
    .sort(
      (left, right) =>
        Date.parse(right.created_at) - Date.parse(left.created_at) ||
        right.id.localeCompare(left.id),
    );
});
const totalPages = computed(() =>
  Math.max(1, Math.ceil(filteredAdjustments.value.length / pageSize)),
);
const currentPage = computed(() => Math.min(page.value, totalPages.value));
const visibleAdjustments = computed(() =>
  filteredAdjustments.value.slice(
    (currentPage.value - 1) * pageSize,
    currentPage.value * pageSize,
  ),
);

function cancelReverse() {
  reversingId.value = null;
  reverseReason.value = "";
}

watch([scope, memberId, status, query, cycleKey], () => {
  page.value = 1;
  cancelReverse();
  if (!props.selectedCycle) scope.value = "all";
});

function changePage(nextPage: number) {
  page.value = nextPage;
  cancelReverse();
}

function startReverse(id: string) {
  reversingId.value = id;
  reverseReason.value = "";
}

function originalAdjustment(row: CPAQuotaAdjustment) {
  return props.adjustments.find((item) => item.id === row.reversal_of);
}
</script>

<template>
  <section class="border-t border-base-300 pt-4" aria-label="入账历史">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h4 class="font-semibold">入账历史</h4>
      <span class="text-xs text-base-content/60">
        账号共 {{ adjustments.length }} 条 · 每页 {{ pageSize }} 条
      </span>
    </div>
    <div class="mt-3 grid gap-2 sm:grid-cols-3">
      <select
        v-model="scope"
        class="select w-full select-sm"
        aria-label="入账周期范围"
      >
        <option v-if="selectedCycle" value="cycle">所选周期</option>
        <option value="all">全部周期</option>
      </select>
      <select
        v-model="memberId"
        class="select w-full select-sm"
        aria-label="入账成员"
      >
        <option value="">全部成员</option>
        <option
          v-for="member in members"
          :key="member.id"
          :value="String(member.id)"
        >
          {{ member.name }}
        </option>
      </select>
      <select
        v-model="status"
        class="select w-full select-sm"
        aria-label="入账状态"
      >
        <option value="all">全部状态</option>
        <option value="applied">已入账</option>
        <option value="reversed">已撤销</option>
        <option value="reversal">反向记录</option>
      </select>
    </div>
    <input
      v-model="query"
      type="search"
      class="input mt-2 w-full input-sm"
      aria-label="搜索入账记录"
      placeholder="搜索成员、原因或操作者"
    />
    <p class="mt-2 text-xs text-base-content/60">
      最近操作在前，展开记录可查看原因与撤销操作。
    </p>

    <div v-if="visibleAdjustments.length" class="mt-3 space-y-2">
      <details
        v-for="row in visibleAdjustments"
        :key="row.id"
        class="collapse-arrow collapse border border-base-300"
      >
        <summary class="collapse-title text-sm">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span class="font-medium">{{ row.participant_name }}</span>
            <span class="font-semibold tabular-nums">
              {{ formatCurrency(row.amount_usd) }}
            </span>
            <span
              class="badge badge-sm"
              :class="
                row.reversal_of || row.reversed
                  ? 'badge-ghost'
                  : 'badge-success'
              "
            >
              {{
                row.reversal_of
                  ? "反向记录"
                  : row.reversed
                    ? "已撤销"
                    : "已入账"
              }}
            </span>
          </div>
          <p class="mt-1 text-xs text-base-content/60">
            操作于 {{ formatTime(row.created_at) }}
          </p>
        </summary>
        <div class="collapse-content space-y-2 text-sm">
          <p class="break-words whitespace-pre-wrap">{{ row.reason }}</p>
          <p class="text-xs text-base-content/60">
            入账时间 {{ formatTime(row.effective_at) }} · 操作者
            {{ row.created_by_name || "未知" }}
          </p>
          <p class="text-xs text-base-content/60">
            官方周期 {{ formatTime(row.cycle_started_at) }} 至
            {{ formatTime(row.cycle_ended_at) }}
          </p>
          <p v-if="row.reversal_of" class="text-xs text-base-content/60">
            此记录撤销原入账
            {{ formatCurrency(originalAdjustment(row)?.amount_usd) }}。
          </p>
          <template
            v-if="row.amount_usd > 0 && !row.reversed && !row.reversal_of"
          >
            <form
              v-if="reversingId === row.id"
              class="space-y-2"
              @submit.prevent="emit('reverse', row.id, reverseReason.trim())"
            >
              <p class="text-xs text-warning">
                撤销将追加等额反向记录，原入账记录会保留。
              </p>
              <textarea
                v-model="reverseReason"
                class="textarea w-full textarea-sm"
                maxlength="500"
                aria-label="撤销原因"
                placeholder="填写本条入账的撤销原因"
                required
              />
              <div class="flex flex-wrap gap-2">
                <button
                  type="submit"
                  class="btn btn-sm btn-warning"
                  :disabled="!reverseReason.trim()"
                >
                  确认撤销
                </button>
                <button
                  type="button"
                  class="btn btn-ghost btn-sm"
                  @click="cancelReverse"
                >
                  取消
                </button>
              </div>
            </form>
            <button
              v-else
              type="button"
              class="btn btn-ghost btn-xs"
              @click="startReverse(row.id)"
            >
              撤销入账
            </button>
          </template>
        </div>
      </details>
    </div>
    <p v-else class="py-6 text-center text-sm text-base-content/60">
      {{
        scope === "cycle" && !scopedAdjustments.length
          ? "所选周期暂无入账记录，可切换全部周期查看。"
          : "没有符合筛选条件的入账记录。"
      }}
    </p>
    <PaginationControls
      :page="currentPage"
      :total-pages="totalPages"
      :total="filteredAdjustments.length"
      show-page-summary
      @change="changePage"
    />
  </section>
</template>
