<script setup lang="ts">
import { computed, ref } from "vue";

import type { GPTLoadAccountOption, MonitoredAccount } from "@/types/accounts";
import type { AppSettingsData } from "@/types/settings";

const settings = defineModel<AppSettingsData>("settings", { required: true });
const authKey = defineModel<string>("authKey", { required: true });
const props = defineProps<{
  accounts: GPTLoadAccountOption[];
  monitoredAccounts: MonitoredAccount[];
  legacyCpaAccounts: MonitoredAccount[];
  loadingAccounts: boolean;
  testing: boolean;
  saving: boolean;
  savingAccountId: number | "new" | null;
}>();
const emit = defineEmits<{
  loadAccounts: [];
  test: [];
  save: [];
  saveAccount: [account: MonitoredAccount, create: boolean];
  cutover: [accountId: number, source: GPTLoadAccountOption];
}>();

const selectedSourceKey = ref("");
const selectedLegacyAccountId = ref<number | null>(null);
const monitoredCredentialIds = computed(
  () =>
    new Set(
      props.monitoredAccounts
        .map((item) => item.gpt_load_credential_id)
        .filter((value): value is number => value != null),
    ),
);
const availableAccounts = computed(() =>
  props.accounts.filter(
    (item) => !monitoredCredentialIds.value.has(item.credential_id),
  ),
);
const selectedSource = computed(() => {
  const [groupId, credentialId] = selectedSourceKey.value
    .split(":")
    .map(Number);
  return props.accounts.find(
    (item) => item.group_id === groupId && item.credential_id === credentialId,
  );
});

function optionKey(account: GPTLoadAccountOption) {
  return `${account.group_id}:${account.credential_id}`;
}

function sourceLabel(account: GPTLoadAccountOption) {
  const identity =
    account.email || account.mask || `Credential ${account.credential_id}`;
  return `${account.group_name} · ${identity}${account.plan_type ? ` · ${account.plan_type}` : ""}`;
}

function addSelectedAccount() {
  const source = selectedSource.value;
  if (!source) return;
  emit(
    "saveAccount",
    {
      id: 0,
      provider: "gpt_load",
      source_account_id: optionKey(source),
      pool_id: 0,
      external_account_id: null,
      cpa_auth_index: null,
      gpt_load_group_id: source.group_id,
      gpt_load_credential_id: source.credential_id,
      gpt_load_cutover_at: null,
      gpt_load_logs_synced_through: null,
      name:
        source.email ||
        source.mask ||
        `${source.group_name} Credential ${source.credential_id}`,
      enabled: true,
      quota_query_mode: "direct",
      quota_profile: "auto",
      detected_plan_type: "",
      effective_quota_profile: "pro_20x",
      capacity_min_usd_override: null,
      capacity_max_usd_override: null,
      capacity_min_usd: 1400,
      capacity_max_usd: 4000,
      last_local_check_at: null,
      last_upstream_check_at: null,
      last_success_at: null,
      next_local_check_at: null,
      last_error: "",
    },
    true,
  );
  selectedSourceKey.value = "";
}

function cutoverSelectedAccount() {
  if (!selectedLegacyAccountId.value || !selectedSource.value) return;
  emit("cutover", selectedLegacyAccountId.value, selectedSource.value);
}
</script>

<template>
  <section
    class="card mb-6 inline-block w-full break-inside-avoid bg-base-200 shadow-xs"
  >
    <div class="card-body gap-5">
      <div>
        <div class="flex flex-wrap items-center gap-2">
          <h2 class="card-title">
            <AppIcon name="server" class="size-5" />GPT-Load 连接与账号
          </h2>
          <span class="badge badge-outline badge-sm">gpt-load</span>
        </div>
        <p class="mt-2 text-sm leading-6 opacity-70">
          从 GPT-Load 管理 API 读取订阅账号、Access Key 和请求日志。CPA
          账号可原地续接，账号主键、额度池、历史合同和本周期已用额度均保持不变。
        </p>
      </div>

      <div class="grid gap-3">
        <fieldset class="fieldset">
          <label class="label">GPT-Load 地址</label>
          <input
            v-model="settings.gpt_load_base_url"
            type="url"
            class="input w-full"
            placeholder="http://gpt-load:3001"
          />
        </fieldset>
        <fieldset class="fieldset">
          <label class="label">AUTH_KEY</label>
          <input
            v-model="authKey"
            type="password"
            class="input w-full"
            :placeholder="
              settings.gpt_load_auth_key_configured
                ? '已配置；留空保持不变'
                : '请输入 GPT-Load AUTH_KEY'
            "
          />
        </fieldset>
        <div class="flex flex-wrap gap-2">
          <button
            class="btn btn-primary btn-sm"
            :disabled="saving"
            @click="emit('save')"
          >
            <span
              v-if="saving"
              class="loading loading-xs loading-spinner"
            ></span>
            保存 GPT-Load 设置
          </button>
          <button class="btn btn-sm" :disabled="testing" @click="emit('test')">
            <span
              v-if="testing"
              class="loading loading-xs loading-spinner"
            ></span>
            测试连接
          </button>
          <button
            class="btn btn-sm"
            :disabled="loadingAccounts"
            @click="emit('loadAccounts')"
          >
            <span
              v-if="loadingAccounts"
              class="loading loading-xs loading-spinner"
            ></span>
            读取订阅账号
          </button>
        </div>
      </div>

      <div class="divider my-0">订阅账号</div>
      <article
        v-for="account in monitoredAccounts"
        :key="account.id"
        class="rounded-box border border-base-300 bg-base-100 p-4"
      >
        <div class="grid gap-3 md:grid-cols-2">
          <fieldset class="fieldset">
            <label class="label">本地显示名称</label>
            <input v-model="account.name" class="input w-full" />
          </fieldset>
          <fieldset class="fieldset">
            <label class="label">Group / Credential</label>
            <input
              :value="`${account.gpt_load_group_id}:${account.gpt_load_credential_id}`"
              class="input w-full font-mono text-xs"
              disabled
            />
          </fieldset>
        </div>
        <p class="mt-2 text-xs opacity-60">
          切换时间：{{ account.gpt_load_cutover_at || "新账号" }}
        </p>
        <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
          <label class="label gap-3">
            <input
              v-model="account.enabled"
              type="checkbox"
              class="toggle toggle-sm"
            />
            启用采样
          </label>
          <button
            class="btn btn-sm"
            :disabled="savingAccountId === account.id"
            @click="emit('saveAccount', account, false)"
          >
            保存账号
          </button>
        </div>
      </article>

      <fieldset class="fieldset">
        <label class="label">选择未纳管的 GPT-Load 订阅账号</label>
        <select v-model="selectedSourceKey" class="select w-full">
          <option value="">请选择 Group / Credential</option>
          <option
            v-for="account in availableAccounts"
            :key="optionKey(account)"
            :value="optionKey(account)"
          >
            {{ sourceLabel(account) }}
          </option>
        </select>
      </fieldset>
      <button
        class="btn btn-sm"
        :disabled="!selectedSource || savingAccountId === 'new'"
        @click="addSelectedAccount"
      >
        新增为独立 GPT-Load 账号
      </button>

      <div
        v-if="legacyCpaAccounts.length"
        class="alert rounded-box alert-warning"
      >
        <div class="w-full space-y-3">
          <div>
            <h3 class="font-semibold">从 CPA 原地续接</h3>
            <p class="text-sm">
              切换后停止接收该 CPA 账号的新事件；历史事实不改写，GPT-Load
              日志从切换时刻继续累计。
            </p>
          </div>
          <select v-model="selectedLegacyAccountId" class="select w-full">
            <option :value="null">选择原 CPA 账号</option>
            <option
              v-for="account in legacyCpaAccounts"
              :key="account.id"
              :value="account.id"
            >
              {{ account.name }} · {{ account.cpa_auth_index }}
            </option>
          </select>
          <button
            class="btn btn-sm btn-warning"
            :disabled="!selectedLegacyAccountId || !selectedSource"
            @click="cutoverSelectedAccount"
          >
            原地续接到所选 GPT-Load 账号
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
