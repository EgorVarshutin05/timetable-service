# Источники и область

Источник требований — реальные TASK, CONTRIBUTING, openspec/config.yaml и
tools/lint_commits.py из исходного архива. В TASK семь критериев K1..K7, K8 нет.
User-confirmed baseline WP-06: flat status/db/broker, 438 unit/PG passed,
Docker app/postgres/people-stub/rabbitmq ready=200, public e2e 3 passed in 9.14s.
Новая накладка меняет только README и OpenSpec. Локальная копия черновика WP-06
приведена к этому baseline до вычисления изменений WP-07.

# Канонические спецификации

catalog-bootstrap — FR-04/FR-11/NFR-02/03; people-client — FR-12/BR-03/NFR-07;
lesson-scheduling — FR-01/02 и BR-01..07; lesson-browsing-and-editing — FR-03/05;
lesson-lifecycle — FR-06..10 и BR-08; lesson-events — FR-13/NFR-01;
quality-and-delivery — NFR-01..07. В Purpose перечислены все идентификаторы явно.
Канон содержит текущее состояние и ## Requirements, а changes — дельты
## ADDED Requirements. У каждого Requirement есть хотя бы один Scenario WHEN/THEN.
Из канона убраны устаревшие future-publisher и вложенный readiness.

# Слои и защита инвариантов

domain хранит чистые BR и статусную машину, api — форму/контракт, repositories — SQL,
services — порядок I/O и commit, broker/clients — интеграционные адаптеры.
BR-01/02/04/05/06/07/08 защищаются доменом, BR-03 — проверкой БД/people-client;
статусы и BR-08 проверяются после row-lock там, где требуется актуальность.
CHECK/FK/UNIQUE миграции защищают структурные инварианты. Advisory transaction locks
в глобальном порядке и FOR UPDATE защищают гонки; события отправляются после commit.
WP-07 только описывает эти существующие механизмы и не добавляет новый инвариант.

# Архив и линтер

Каталоги WP-01..06 перемещаются git mv в archive/2026-11-15-<id>; изменения их
завершённых tasks и финального WP-06 контракта коммитятся вместе с каноном.
Ветки change/<id> сохраняются. change_exists_in_rev поддерживает пути archive.
Линтер не содержит специальной ветви для wp07: разрешены spec/docs/chore,
scopes openspec/readme; обязательного feat/test нет. spec/tests order=no-code —
только статистика, а не WARN. Все headers <=72. Merge — docs(readme) с описанием,
Change: wp07-finalize-docs и Refs: NFR-05, NFR-06. Полная история проверяется
на master после merge; на feature HEAD spec-коммит ошибочно попадёт в first-parent
роль прямого master, поэтому до merge используют --head master или message mode.

# Доказательства и время

Свежие ruff/mypy/pytest и message/history checks выполняются локально. Реальный
Docker/e2e результат атрибутирован пользователю, повторный запуск ассистентом
не выдумывается. Отдельная модель git-истории проверяет предлагаемый docs-only merge
и последующий архив; она не подменяет недоступную пользовательскую историю.
Факты WP-00..06 фиксированы точно; их сумма 4,46 ч. Итог Must = 4,46 + факт WP-07;
при WP-07=0,30 получается 4,76. Обе реальные ячейки остаются пустыми до ввода автора.
