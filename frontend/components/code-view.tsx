import type { ReactNode } from "react";

// Small lexical highlighter. React escapes every token, including generated HTML.
// Unknown file extensions remain plain text; this never evaluates generated code.
const patterns = {
  python:
    /(?<comment>#[^\n]*)|(?<string>"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(?<keyword>\b(?:from|import|as|def|return|if|else|elif|for|in|and|or|not|is|None|True|False|class|with|try|except|raise|lambda|pass|yield)\b)|(?<decorator>@[\w.]+)|(?<number>\b\d+(?:\.\d+)?\b)/g,
  sql: /(?<comment>--[^\n]*|\/\*[\s\S]*?\*\/)|(?<string>'(?:''|[^'])*'|"(?:""|[^"])*"|`[^`]*`)|(?<keyword>\b(?:CREATE|OR|REFRESH|LIVE|STREAMING|TABLE|MATERIALIZED|VIEW|AS|SELECT|FROM|WHERE|AND|NOT|NULL|IS|JOIN|LEFT|RIGHT|INNER|ON|GROUP|BY|ORDER|HAVING|SUM|COUNT|AVG|DISTINCT|CAST|CASE|WHEN|THEN|ELSE|END|CONSTRAINT|EXPECT|DROP|ROW|FAIL|UPDATE|SET|UNION|ALL|TRIM|LOWER)\b)|(?<number>\b\d+(?:\.\d+)?\b)/gi,
  yaml: /(?<comment>#[^\n]*)|(?<string>"(?:\\.|[^"\\\n])*"|'(?:''|[^'\n])*')|(?<key>[\w.-]+(?=\s*:))|(?<keyword>\b(?:true|false|null)\b)|(?<number>\b\d+(?:\.\d+)?\b)/g,
};

export function codeLanguage(path: string) {
  if (/\.py$/i.test(path)) return "python";
  if (/\.sql$/i.test(path)) return "sql";
  if (/\.ya?ml$/i.test(path)) return "yaml";
  return "text";
}

export function CodeView({ path, content }: { path: string; content: string }) {
  const language = codeLanguage(path);
  const tokens: ReactNode[] = [];
  let offset = 0;
  if (language !== "text") {
    for (const match of content.matchAll(patterns[language])) {
      const start = match.index;
      tokens.push(content.slice(offset, start));
      const kind = Object.keys(match.groups ?? {}).find(
        (name) => match.groups?.[name] !== undefined,
      );
      tokens.push(
        <span key={start} className={`syntax-${kind}`}>
          {match[0]}
        </span>,
      );
      offset = start + match[0].length;
    }
  }
  tokens.push(content.slice(offset));
  return (
    <pre className="code-content" tabIndex={0} aria-label={`${path} 文件内容`}>
      <code className={`language-${language}`}>{tokens}</code>
    </pre>
  );
}
