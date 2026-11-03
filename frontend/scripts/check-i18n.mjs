// Checks the English interface text (lib/i18n/en.json). Run by `npm run lint`.
//
// 1. Every t("…") with literal Chinese text anywhere has an English entry, with the same
//    {placeholders}.
// 2. In the files listed in TRANSLATED, no Chinese text is left untranslated: every
//    Chinese string literal has an English entry (it is shown through t()), and there is
//    no Chinese JSX text or Chinese template string (use t("…{name}…", { name })).
//    A line ending in `// i18n-ignore` is skipped.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = fileURLToPath(new URL("..", import.meta.url));
const en = JSON.parse(readFileSync(join(root, "lib/i18n/en.json"), "utf8"));

// Files whose interface text is fully translated. Add a file once it is done.
const TRANSLATED = [
  "app/layout.tsx",
  "components/app-shell.tsx",
  "components/asset-ui.tsx",
  "components/catalog-list.tsx",
  "components/datasource-detail.tsx",
  "components/datasource-list.tsx",
  "components/input-fields.tsx",
  "components/knowledge-list.tsx",
  "components/locale-switch.tsx",
  "components/login-page.tsx",
  "components/model-list.tsx",
  "components/new-run.tsx",
  "components/notification-bell.tsx",
  "components/notification-list.tsx",
  "components/password-dialog.tsx",
  "components/project-list.tsx",
  "components/run-list.tsx",
  "components/skill-list.tsx",
  "components/ui.tsx",
  "lib/auth.ts",
  "lib/i18n/format.ts",
  "lib/notifications.ts",
];

const CJK = /[㐀-鿿＀-￯　-〿]/;
const HAN = /[㐀-鿿]/;
const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(",");

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (["node_modules", ".next", "tests", "scripts", "docs"].includes(name)) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(path);
  }
  return out;
}

const errors = [];
for (const [key, value] of Object.entries(en)) {
  if (typeof value !== "string" || !value.trim())
    errors.push(`lib/i18n/en.json: empty translation for ${JSON.stringify(key)}`);
  else if (placeholders(key) !== placeholders(value))
    errors.push(`lib/i18n/en.json: placeholders differ for ${JSON.stringify(key)}`);
  else if (HAN.test(value))
    errors.push(`lib/i18n/en.json: Chinese left in the translation of ${JSON.stringify(key)}`);
}

const files = ["app", "components", "lib"].flatMap((dir) => walk(join(root, dir)));
for (const file of files) {
  const rel = relative(root, file).replaceAll("\\", "/");
  if (rel.startsWith("lib/server/mock")) continue;
  const text = readFileSync(file, "utf8");
  const lines = text.split("\n");
  const strict = TRANSLATED.includes(rel);
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const report = (node, message) => {
    const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
    if (lines[line].includes("i18n-ignore")) return;
    errors.push(`${rel}:${line + 1}: ${message}`);
  };
  const visit = (node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === "t" &&
      node.arguments[0] &&
      ts.isStringLiteralLike(node.arguments[0]) &&
      HAN.test(node.arguments[0].text) &&
      !(node.arguments[0].text in en)
    ) {
      report(node, `no English for t(${JSON.stringify(node.arguments[0].text)})`);
    }
    if (strict) {
      if (ts.isStringLiteralLike(node) && HAN.test(node.text) && !(node.text in en))
        report(node, `no English for ${JSON.stringify(node.text)}`);
      if (ts.isTemplateExpression(node) && CJK.test(node.getText(source)))
        report(node, "Chinese in a template string: use t(\"…{name}…\", { name })");
      if (ts.isJsxText(node) && HAN.test(node.text))
        report(node, `Chinese JSX text not in t(): ${JSON.stringify(node.text.trim())}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
}

if (errors.length) {
  console.error(`i18n check: ${errors.length} problem(s)\n${errors.join("\n")}`);
  process.exit(1);
}
console.log(`i18n check: ${Object.keys(en).length} English entries, ${TRANSLATED.length} translated files OK`);
