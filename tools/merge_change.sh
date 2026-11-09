#!/usr/bin/env bash
# Слияние завершённого пакета change/<id> в master merge-коммитом (--no-ff) с шаблоном сообщения.
# Вызывается рецептом `just merge-change <id>`; см. CONTRIBUTING.md §3.
#
# Шаги: проверка имени и ветки → чистое рабочее дерево → git switch master →
# git merge --no-ff --no-commit → проверка результата слияния (по умолчанию `just check`,
# переопределяется переменной MERGE_CHECK) → редактор с шаблоном сообщения, в котором
# footers Change и Refs заполнены по текстам OpenSpec change.
set -euo pipefail

id="${1:-}"
if [[ -z "$id" ]]; then
  echo "использование: tools/merge_change.sh wpNN-<slug>" >&2
  exit 2
fi
if [[ ! "$id" =~ ^wp[0-9]{2}-[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "идентификатор пакета должен быть вида wp03-detect-lesson-conflicts, получено: $id" >&2
  exit 2
fi

branch="change/$id"
if ! git rev-parse --verify --quiet "refs/heads/$branch" >/dev/null; then
  echo "ветка $branch не найдена" >&2
  exit 2
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "рабочее дерево не чисто: закоммитьте или уберите изменения перед слиянием" >&2
  exit 2
fi

git switch master
if ! git merge --no-ff --no-commit "$branch"; then
  git merge --abort
  echo "конфликт слияния: перебазируйте $branch на master (git rebase master), разрешите конфликты и повторите" >&2
  exit 1
fi
if ! git rev-parse --quiet --verify MERGE_HEAD >/dev/null; then
  echo "ветка $branch уже слита в master — делать нечего"
  exit 0
fi

check_cmd="${MERGE_CHECK:-just check}"
if ! $check_cmd; then
  git merge --abort
  echo "проверка «$check_cmd» на результате слияния не прошла — слияние отменено" >&2
  exit 1
fi

change_dir=""
for candidate in "openspec/changes/$id" openspec/changes/archive/*-"$id"; do
  if [[ -d "$candidate" ]]; then
    change_dir="$candidate"
    break
  fi
done
refs="FR-xx"
if [[ -n "$change_dir" ]]; then
  found="$(grep -rhoE '\bN?FR-[0-9]{2}\b' "$change_dir" 2>/dev/null | sort -u | paste -sd ',' - | sed 's/,/, /g' || true)"
  if [[ -n "$found" ]]; then
    refs="$found"
  fi
fi

msg="$(mktemp)"
trap 'rm -f "$msg"' EXIT
cat > "$msg" <<EOF
feat(<scope>): <что делает пакет $id>

<Что изменилось и почему; ключевые решения>

Как проверить: just test; just e2e-public

Change: $id
Refs: $refs

# Заголовок — по Conventional Commits (type по содержанию пакета, обычно feat).
# Тело — описание изменения, как в merge request. Footers Change и Refs обязательны.
# Плейсхолдеры в угловых скобках нужно заменить; строки с # будут удалены.
EOF

if ! git commit --edit --file="$msg"; then
  echo "merge-коммит не создан (сообщение отклонено или редактор закрыт без сохранения)." >&2
  echo "Слияние остаётся подготовленным: исправьте сообщение и выполните git commit, либо git merge --abort." >&2
  exit 1
fi
echo "пакет $id слит в master; ветку не удаляйте — git push --all"
