import { AgentCommandResult, CommandReceipt, readAgentCommandResult, submitAgentCommand } from "./bridge";

// A catalog update can download two Release assets through slow TLS redirects.
const RESULT_TIMEOUT_MS = 90_000;
const RESULT_POLL_MS = 100;

function pause(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

/** Owns command admission and result polling for all desktop UI interactions. */
export async function requestAgentCommand(
  commandType: string,
  payload: Record<string, unknown>,
): Promise<AgentCommandResult> {
  if (commandType === "metrics.query" && window.__INFRA_SENTINEL_STATIC_DEMO_LOCALE) {
    return {
      schema: "static-demo",
      id: "static-demo-metrics",
      type: commandType,
      status: "ok",
      payload: { points: [] },
    };
  }
  const receipt: CommandReceipt = await submitAgentCommand(commandType, payload);
  const deadline = Date.now() + RESULT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const result = await readAgentCommandResult(receipt.id);
    if (result) return result;
    await pause(RESULT_POLL_MS);
  }
  throw new Error("The Infra Agent did not complete the request within 15 seconds.");
}
