import type { MonitoredAccount } from "@/types/accounts";

export function monitoredAccountLabel(account: MonitoredAccount): string {
  if (account.provider === "cpa") return `${account.name} · CPA`;
  if (account.provider === "gpt_load") return `${account.name} · GPT-Load`;
  return account.name;
}
