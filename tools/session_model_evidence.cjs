// Inspect only a workbench-created session supplied by the acceptance runner.
// Output model-selection fields, never messages, tool bodies, or reasoning.
const fs = require('node:fs');
const zlib = require('node:zlib');
const buffer = fs.readFileSync(process.argv[2]);
let offset = 0, content = '';
while (offset < buffer.length) {
  const frame = zlib.zstdDecompressSync(buffer.subarray(offset), {info: true});
  if (!frame.engine.bytesWritten) throw new Error('Invalid zstd frame');
  offset += frame.engine.bytesWritten;
  content += frame.buffer.toString('utf8');
}
const selections = [];
const allowed = new Set(['provider', 'providerId', 'model', 'modelId', 'reasoningEffort']);
function walk(value, fields) {
  if (!value || typeof value !== 'object') return;
  if (Array.isArray(value)) { value.forEach(item => walk(item, fields)); return; }
  for (const [key, item] of Object.entries(value)) {
    if (allowed.has(key) && typeof item === 'string' && item.length < 200) fields[key] = item;
    else if (!['messages','message','inserted','content','text','thinking','reasoning','result','output','arguments'].includes(key)) walk(item, fields);
  }
}
content.split('\n').filter(Boolean).forEach((line, index) => {
  const record = JSON.parse(line);
  const fields = {};
  walk(record, fields);
  if (Object.keys(fields).length) selections.push({record: index, type: record.type, ...fields});
});
console.log(JSON.stringify({selections}));
