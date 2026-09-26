<script setup lang="ts">
import { computed, ref } from "vue";
import { api, jsonBody } from "@/services/api";
import { useAuthStore } from "@/stores/auth";
import type {
  CPAQuotaAdjustmentPlan,
  CPAQuotaDiscrepancyAudit,
  CPAQuotaDiscrepancyAdmin,
  CPAPoolSummary,
} from "@/types/cpa";
import { formatCurrency } from "@/utils/formatters";
import { useDateTime } from "@/composables/useDateTime";

const props = defineProps<{
  account: CPAPoolSummary["accounts"][number];
}>();
const emit = defineEmits<{ refresh: [] }>();
const auth = useAuthStore();
const formatTime = useDateTime();
const dialog = ref<HTMLDialogElement | null>(null);
const loading = ref(false);
const message = ref("");
const adminData = ref<CPAQuotaDiscrepancyAdmin | null>(null);
const selectedObservationId = ref<number | null>(null);
const participantId = ref<number | null>(null);
const amount = ref<number>(0);
const reason = ref("");
const reverseReason = ref("");
const plan = ref<CPAQuotaAdjustmentPlan | null>(null);

const selectedCycle = computed<CPAQuotaDiscrepancyAudit | null>(
  () =>
    adminData.value?.cycles.find(
      (row) => row.latest_observation_id === selectedObservationId.value,
    ) ?? null,
);
const eligibleParticipants = computed(() =>
  (adminData.value?.participants ?? []).filter((row) =>
    selectedCycle.value?.eligible_participant_ids.includes(row.id),
  ),
);

function resetForm(cycle: CPAQuotaDiscrepancyAudit | null) {
  selectedObservationId.value = cycle?.latest_observation_id ?? null;
  participantId.value =
    cycle?.eligible_participant_ids[0] ??
    adminData.value?.participants[0]?.id ??
    null;
  amount.value = cycle
    ? cycle.remaining_suggested_usd > 0
      ? cycle.remaining_suggested_usd
      : cycle.remaining_upper_usd
    : 0;
  reason.value = "";
  plan.value = null;
}

async function load() {
  loading.value = true;
  message.value = "";
  try {
    adminData.value = await api<CPAQuotaDiscrepancyAdmin>(
      `cpa/quota-discrepancies?account_id=${props.account.account_id}`,
    );
    const preferred =
      [...adminData.value.cycles].reverse().find((row) => row.can_attribute) ??
      adminData.value.cycles.at(-1) ??
      null;
    resetForm(preferred);
  } catch (error) {
    message.value = error instanceof Error ? error.message : "读取差额失败";
  } finally {
    loading.value = false;
  }
}

async function open() {
  dialog.value?.showModal();
  await load();
}

function changeCycle() {
  resetForm(selectedCycle.value);
}

async function preview() {
  if (!selectedCycle.value || participantId.value == null) return;
  loading.value = true;
  message.value = "";
  plan.value = null;
  try {
    plan.value = await api<CPAQuotaAdjustmentPlan>(
      `cpa/quota-adjustments/preview?account_id=${props.account.account_id}`,
      {
        method: "POST",
        body: jsonBody({
          participant_id: participantId.value,
          latest_observation_id: selectedCycle.value.latest_observation_id,
          amount_usd: amount.value,
          reason: reason.value,
        }),
      },
    );
  } catch (error) {
    message.value = error instanceof Error ? error.message : "预览失败";
  } finally {
    loading.value = false;
  }
}

async function apply() {
  if (!plan.value) return;
  loading.value = true;
  try {
    await api(`cpa/quota-adjustments/${plan.value.id}/apply`, {
      method: "POST",
    });
    message.value = "人工归因已正式入账。";
    plan.value = null;
    await load();
    emit("refresh");
  } catch (error) {
    message.value = error instanceof Error ? error.message : "应用失败";
    plan.value = null;
  } finally {
    loading.value = false;
  }
}

async function reverse(id: string) {
  if (!reverseReason.value.trim()) {
    message.value = "请先填写撤销原因。";
    return;
  }
  loading.value = true;
  try {
    await api(`cpa/quota-adjustments/${id}/reverse`, {
      method: "POST",
      body: jsonBody({ reason: reverseReason.value }),
    });
    reverseReason.value = "";
    message.value = "已追加反向记录。";
    await load();
    emit("refresh");
  } catch (error) {
    message.value = error instanceof Error ? error.message : "撤销失败";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div
    v-if="account.quota_discrepancy || auth.isStaff"
    class="mt-3 rounded-box bg-base-200 p-3"
  >
    <p class="text-sm font-medium">未解释额度变化</p>
    <p
      v-if="account.quota_discrepancy"
      class="mt-1 text-xs text-base-content/70"
    >
      请求日志
      {{ formatCurrency(account.quota_discrepancy.request_usage_usd) }}；
      差额建议
      {{ formatCurrency(account.quota_discrepancy.remaining_suggested_usd) }}
      （范围
      {{ formatCurrency(account.quota_discrepancy.remaining_lower_usd) }}～{{
        formatCurrency(account.quota_discrepancy.remaining_upper_usd)
      }}）。
    </p>
    <p
      v-if="(account.quota_discrepancy?.held_unexplained_usd ?? 0) > 0"
      class="mt-1 text-xs text-warning"
    >
      其中
      {{ formatCurrency(account.quota_discrepancy?.held_unexplained_usd ?? 0) }}
      暂时冻结，不计入任何成员用量。按历史份额分配后，
      {{
        formatCurrency(account.quota_discrepancy?.unallocated_hold_usd ?? 0)
      }}保留在账号级。
    </p>
    <p
      v-if="account.quota_discrepancy?.over_attributed"
      class="mt-1 text-xs text-error"
    >
      已确认人工归因超过当前估算，请检查历史调整。
    </p>
    <p
      v-if="account.quota_discrepancy?.reasons.length"
      class="mt-1 text-xs text-base-content/60"
    >
      {{ account.quota_discrepancy.reasons.join("；") }}
    </p>
    <p
      v-if="!account.quota_discrepancy && auth.isStaff"
      class="mt-1 text-xs text-base-content/60"
    >
      当前没有可展示的额度差额；仍可查看历史周期和调整记录。
    </p>
    <button v-if="auth.isStaff" class="btn mt-2 btn-sm" @click="open">
      核对额度差额
    </button>
  </div>

  <dialog
    ref="dialog"
    class="modal"
    @cancel="loading && $event.preventDefault()"
  >
    <div class="modal-box w-[calc(100vw-2rem)] max-w-3xl">
      <h3 class="text-lg font-semibold">
        {{ account.account_name }} · 人工额度归因
      </h3>
      <p class="mt-2 text-sm text-base-content/60">
        这里只确认没有请求明细的额度差异。确认后会正式进入成员账期，但不会生成请求或
        Token。
      </p>
      <p v-if="message" class="mt-3 alert text-sm" role="status">
        {{ message }}
      </p>
      <p v-if="loading" class="mt-3" role="status">正在处理…</p>

      <div v-if="adminData && !loading" class="mt-4 space-y-4">
        <fieldset class="fieldset">
          <label class="label">官方周期</label>
          <select
            v-model.number="selectedObservationId"
            class="select w-full"
            @change="changeCycle"
          >
            <option
              v-for="cycle in adminData.cycles"
              :key="cycle.latest_observation_id"
              :value="cycle.latest_observation_id"
            >
              {{ formatTime(cycle.cycle_started_at) }} 至
              {{ formatTime(cycle.cycle_ended_at) }} ·
              {{ cycle.can_attribute ? "可核对" : "证据不足" }}
            </option>
          </select>
        </fieldset>

        <div v-if="selectedCycle" class="rounded-box bg-base-200 p-4 text-sm">
          <p>官方增量 {{ selectedCycle.official_delta_percent.toFixed(2) }}%</p>
          <p>
            同期请求日志 {{ formatCurrency(selectedCycle.request_usage_usd) }}
          </p>
          <p>
            未认领建议
            {{ formatCurrency(selectedCycle.remaining_suggested_usd) }}，范围
            {{ formatCurrency(selectedCycle.remaining_lower_usd) }}～{{
              formatCurrency(selectedCycle.remaining_upper_usd)
            }}
          </p>
          <p v-if="selectedCycle.reasons.length" class="mt-1 text-warning">
            {{ selectedCycle.reasons.join("；") }}
          </p>
        </div>

        <div
          v-if="selectedCycle?.can_attribute"
          class="grid gap-3 sm:grid-cols-2"
        >
          <fieldset class="fieldset">
            <label class="label">归因成员</label>
            <select v-model.number="participantId" class="select w-full">
              <option
                v-for="member in eligibleParticipants"
                :key="member.id"
                :value="member.id"
              >
                {{ member.name }}
              </option>
            </select>
          </fieldset>
          <fieldset class="fieldset">
            <label class="label">确认金额</label>
            <input
              v-model.number="amount"
              type="number"
              min="0.000001"
              :max="selectedCycle.remaining_upper_usd"
              step="0.01"
              class="input w-full"
            />
          </fieldset>
          <fieldset class="fieldset sm:col-span-2">
            <label class="label">确认原因</label>
            <textarea
              v-model="reason"
              maxlength="500"
              class="textarea w-full"
              required
            />
          </fieldset>
          <button
            class="btn justify-self-start"
            :disabled="loading"
            @click="preview"
          >
            预览正式入账
          </button>
        </div>

        <div v-if="plan" class="alert block text-sm alert-warning">
          <p class="font-semibold">请再次确认</p>
          <p>
            {{ plan.participant_name }} 将增加
            {{ formatCurrency(plan.amount_usd) }}
            人工用量；请求数和 Token 不变。
          </p>
          <button
            class="btn mt-2 btn-sm btn-warning"
            :disabled="loading"
            @click="apply"
          >
            确认正式入账
          </button>
        </div>

        <section
          v-if="adminData.adjustments.length"
          class="border-t border-base-300 pt-4"
        >
          <h4 class="font-semibold">调整历史</h4>
          <input
            v-model="reverseReason"
            class="input mt-2 w-full input-sm"
            maxlength="500"
            placeholder="撤销前填写原因"
          />
          <div class="mt-2 space-y-2">
            <div
              v-for="row in adminData.adjustments"
              :key="row.id"
              class="rounded-box border border-base-300 p-3 text-sm"
            >
              <p>
                {{ row.participant_name }} ·
                {{ formatCurrency(row.amount_usd) }} ·
                {{ formatTime(row.created_at) }}
              </p>
              <p class="text-xs text-base-content/60">{{ row.reason }}</p>
              <button
                v-if="row.amount_usd > 0 && !row.reversed"
                class="btn mt-2 btn-ghost btn-xs"
                :disabled="loading"
                @click="reverse(row.id)"
              >
                追加反向记录
              </button>
            </div>
          </div>
        </section>
      </div>
      <div class="modal-action">
        <button class="btn" :disabled="loading" @click="dialog?.close()">
          关闭
        </button>
      </div>
    </div>
  </dialog>
</template>
