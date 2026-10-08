#!/usr/bin/env node
// In-process timing loop for the TypeScript assembler (domain-1-plan.md, 8.3, method 3).
// Snapshot bytes on stdin; arguments: warm-up runs, then timed runs. Each run goes from the snapshot's bytes to the
// assembler's payload and trace, as the adapter does, without starting a process. Prints one JSON line:
// {"outcome": "assembled" | "refused" | "rejected" | "unsupported", "samples_ns": [...]}.
import { pathToFileURL } from 'node:url';

const { assemble, SnapshotRejectedError, UnsupportedComponentError } =
  await import(pathToFileURL(process.env.CWA_TS_ASSEMBLER).href);
const [warmup, runs] = process.argv.slice(2).map(Number);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const raw = Buffer.concat(chunks);

function once() {
  try {
    const { payload } = assemble(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw)));
    return payload ? 'assembled' : 'refused';
  } catch (error) {
    if (error instanceof SnapshotRejectedError || error instanceof SyntaxError) return 'rejected';
    if (error instanceof UnsupportedComponentError) return 'unsupported';
    throw error;
  }
}

let outcome = null;
for (let i = 0; i < warmup; i++) outcome = once();
const samples = [];
for (let i = 0; i < runs; i++) {
  const started = process.hrtime.bigint();
  outcome = once();
  samples.push(Number(process.hrtime.bigint() - started));
}
process.stdout.write(JSON.stringify({ outcome, samples_ns: samples }) + '\n');
