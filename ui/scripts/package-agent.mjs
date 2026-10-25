// The single build owner for the Python sidecar on each native host.
import { execFileSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const windows = process.platform === "win32";
const extension = windows ? ".exe" : "";
const backend = { win32: "windows", darwin: "macos", linux: "linux" }[process.platform];
if (!backend) throw new Error(`No Agent backend for ${process.platform}`);
const venvPython = join(root, ".venv", windows ? "Scripts/python.exe" : "bin/python");
const python = process.env.PYTHON3 || (existsSync(venvPython) ? venvPython : windows ? "python" : "python3");
const host = execFileSync("rustc", ["-vV"], { encoding: "utf8" }).match(/^host: (.+)$/m)?.[1].trim();
const target = process.argv[2] || process.env.TAURI_ENV_TARGET_TRIPLE || host;
if (!host || target !== host) {
  throw new Error("Build the PyInstaller Agent on its target OS and architecture; cross-compilation is unsupported.");
}
const build = join(root, ".build", "tauri-agent");
const binaries = join(root, "ui", "src-tauri", "binaries");
mkdirSync(build, { recursive: true });
mkdirSync(binaries, { recursive: true });
try {
  execFileSync(python, ["-c", "import PyInstaller, certifi"], { stdio: "pipe" });
} catch {
  throw new Error(`Install the Agent build dependencies with ${python} -m pip install ".[dsh]" pyinstaller`);
}
execFileSync(python, [
  "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
  "--name", "infra-agent",
  "--distpath", join(build, "dist"),
  "--workpath", join(build, "work"),
  "--specpath", join(build, "spec"),
  "--paths", join(root, "src"),
  "--add-data", `${join(root, "src/infra_sentinel/resources/ai/api-price.toml")}:infra_sentinel/resources/ai`,
  "--collect-data", "certifi",
  "--hidden-import", "infra_sentinel.cli.snapshot",
  "--hidden-import", `infra_sentinel.resources.system.backends.${backend}`,
  join(root, "bin", "infra_agent.py"),
], { cwd: root, stdio: "inherit" });
const output = join(binaries, `infra-agent-${target}${extension}`);
copyFileSync(join(build, "dist", `infra-agent${extension}`), output);
if (!windows) chmodSync(output, 0o755);
console.log(`Packaged Agent: ${output}`);
