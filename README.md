# timetable-service

<!--
Шаблон README из задания (TASK.md, Приложение A). Шесть заголовков второго уровня фиксированы и
проверяются автоматически; текст под ними — ваш. Описание того, что уже дано в каркасе, — в SCAFFOLD.md.
-->

## Запуск

<!-- Команды запуска, выбранный брокер (и почему), как проверить готовность. Ниже — то, что уже работает в каркасе. -->

```bash
just doctor      # проверка окружения: Docker, Compose v2.20+, buildx 0.17+, uv 0.13+
docker compose up --build   # так стек запускает проверка; .env не нужен
just up          # то же в фоне с ожиданием healthcheck: app, postgres, people-stub, брокер
just migrate     # alembic upgrade head в контейнере app (entrypoint делает это и при старте)
just seed        # загрузка seed (python -m app.seed)
just check       # ruff + mypy + pytest tests/unit
just e2e-public  # публичные e2e против поднятого стека → e2e-report.json
just setup       # шаблон коммита и хук проверки (CONTRIBUTING.md)
curl -s localhost:8000/health/ready
```

Брокер: RabbitMQ (по умолчанию в docker-compose.yml) — ...

## Решения

<!-- Принятые решения с альтернативами и причинами, включая решения по ситуациям, которые задание не регулирует. -->

## Не сделано

<!-- Что не сделано и почему. -->

## Async-кейс

<!-- Что выполняется параллельно, как воспроизвести замер (команда), полученные цифры. -->

## AI notes

<!-- Инструменты, где применялись, что пришлось исправлять, что отвергнуто и почему. -->

## Время

<!-- По пакетам из Приложения C TASK.md; «Моя оценка» — до начала пакета; факт — с точностью 0,25 ч;
комментарий — при факте ≥ 2× оценки и ≥ +1 ч (CONTRIBUTING.md §4). -->

| Пакет | Оценка задания, ч | Моя оценка до старта, ч | Факт, ч | Куда ушло время (при факте ≥ 2× и ≥ +1 ч) |
| --- | --- | --- | --- | --- |
| WP-00 Процесс | 0,75 | 0,75 | 0,50 | |
| wp01-load-seed-data | 0,80 | 1,00 | | |
| wp02-add-people-client | 0,75 | 0,75 | | |
| wp03-detect-lesson-conflicts | 1,75 | 2,00 | | |
| wp04-list-and-edit-lessons | 0,80 | 1,00 | | |
| wp05-manage-lesson-lifecycle | 1,50 | 1,75 | | |
| wp06-publish-lesson-events | 0,70 | 0,75 | | |
| wp07-finalize-docs | 0,40 | 0,50 | | |
| Итого Must, включая WP-00 | 7,45 | 8,50 | | |
| wp08-create-lesson-series | 1,00 | 1,25 | | |
| wp09-report-instructor-workload | 0,50 | 0,75 | | |
| wp10-list-groups-cache-people | 0,50 | 0,75 | | |
| Итого Should/Could | 2,00 | 2,75 | | |
| Итого | 9,45 | 11,25 | | |
