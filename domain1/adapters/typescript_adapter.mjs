#!/usr/bin/env node
// Adapter from the TypeScript assembler to the harness protocol (assembler-template/PORTING.md).
// Snapshot bytes on stdin. Exit 0 with {"payload": base64 or null, "trace": {...}} when assembled or refused;
// exit 2 when the snapshot is rejected before assembly (the problems on stderr); exit 3 when it names a
// tokenizer or renderer this assembler does not provide.
//
// Adapted from assembler-demo/adapters/typescript_adapter.mjs: the assembler is loaded from the built dist/ that
// CWA_TS_ASSEMBLER names, so the benchmark needs no package link of its own.
import { pathToFileURL } from 'node:url';

const entry = process.env.CWA_TS_ASSEMBLER;
if (!entry) {
  console.error('CWA_TS_ASSEMBLER must name the assembler checkout\'s dist/index.js');
  process.exit(70);
}
const { assemble, SnapshotRejectedError, UnsupportedComponentError } = await import(pathToFileURL(entry).href);

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
let value;
try {
  // fatal: an invalid UTF-8 byte is a malformed snapshot, not a U+FFFD to assemble.
  value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
} catch (error) {
  console.error(`not a JSON document: ${error.message}`);
  process.exit(2);
}
try {
  const { payload, trace } = assemble(value);
  process.stdout.write(JSON.stringify({ payload: payload ? Buffer.from(payload).toString('base64') : null, trace }));
} catch (error) {
  if (error instanceof SnapshotRejectedError) { console.error(error.problems.join('; ')); process.exit(2); }
  if (error instanceof UnsupportedComponentError) { console.error(error.message); process.exit(3); }
  throw error;
}
