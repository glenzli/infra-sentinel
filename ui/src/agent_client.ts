import { AgentCommandResult, CommandReceipt, readAgentCommandResult, submitAgentCommand } from "./bridge";

const RESULT_TIMEOUT_MS = 15_000;
const RESULT_POLL_MS = 100;

function pause(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

/** Owns command admission and result polling for all desktop UI interactions. */
export async function requestAgentCommand(
  commandType: string,
  payload: Record<string, unknown>,
): Promise<AgentCommandResult> {
  const receipt: CommandReceipt = await submitAgentCommand(commandType, payload);
  const deadline = Date.now() + RESULT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const result = await readAgentCommandResult(receipt.id);
    if (result) return result;
    await pause(RESULT_POLL_MS);
  }
  throw new Error("The Infra Agent did not complete the request within 15 seconds.");
}
