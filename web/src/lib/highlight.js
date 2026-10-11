// Minimal Python and YAML tokenizers.
//
// A full highlighter library would be an order of magnitude more code and
// bundle weight than this view needs: the only two languages that ever
// appear here are the generated notebook and its config.

const PY_KEYWORDS = new Set([
  'and', 'as', 'assert', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
  'False', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'None',
  'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'True', 'try', 'while', 'with', 'yield',
]);

function pushToken(tokens, text, cls) {
  if (!text) return;
  const last = tokens[tokens.length - 1];
  if (last && last.cls === cls) last.text += text;
  else tokens.push({ text, cls });
}

export function tokenizePython(line) {
  const tokens = [];

  // Databricks cell and magic markers are structure, not code.
  if (/^\s*#\s*(COMMAND\s*-+|Databricks notebook source|MAGIC\b)/.test(line)) {
    return [{ text: line, cls: 'tok-magic' }];
  }

  let i = 0;
  while (i < line.length) {
    const rest = line.slice(i);

    const comment = rest.match(/^#.*/);
    if (comment) {
      pushToken(tokens, comment[0], 'tok-com');
      break;
    }

    const string = rest.match(/^(?:[rfbu]{0,2})("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?)/);
    if (string) {
      pushToken(tokens, string[0], 'tok-str');
      i += string[0].length;
      continue;
    }

    const number = rest.match(/^\d[\d_]*(\.\d+)?/);
    if (number) {
      pushToken(tokens, number[0], 'tok-num');
      i += number[0].length;
      continue;
    }

    const word = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (word) {
      const following = line.slice(i + word[0].length);
      let cls = null;
      if (PY_KEYWORDS.has(word[0])) cls = 'tok-kw';
      else if (following.startsWith('(')) cls = 'tok-fn';
      pushToken(tokens, word[0], cls);
      i += word[0].length;
      continue;
    }

    pushToken(tokens, line[i], null);
    i += 1;
  }
  return tokens;
}

export function tokenizeYaml(line) {
  const comment = line.match(/^(\s*)(#.*)$/);
  if (comment) {
    return [{ text: comment[1], cls: null }, { text: comment[2], cls: 'tok-com' }];
  }

  const keyed = line.match(/^(\s*-?\s*)([A-Za-z_][\w.-]*)(\s*:)(.*)$/);
  if (keyed) {
    const tokens = [
      { text: keyed[1], cls: null },
      { text: keyed[2], cls: 'tok-key' },
      { text: keyed[3], cls: null },
    ];
    const value = keyed[4];
    const inline = value.match(/^(.*?)(\s+#.*)$/);
    if (inline) {
      tokens.push({ text: inline[1], cls: 'tok-str' });
      tokens.push({ text: inline[2], cls: 'tok-com' });
    } else if (value.trim()) {
      tokens.push({ text: value, cls: 'tok-str' });
    }
    return tokens;
  }

  return [{ text: line, cls: line.trim().startsWith('-') ? 'tok-str' : null }];
}

export function tokenize(line, language) {
  if (language === 'python') return tokenizePython(line);
  if (language === 'yaml') return tokenizeYaml(line);
  return [{ text: line, cls: null }];
}
