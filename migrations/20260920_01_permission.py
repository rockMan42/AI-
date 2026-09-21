from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.permission import (
    AuditLog,
    AuthSession,
    DenialCounter,
    OrganizationState,
    PermissionAlert,
    PermissionEvent,
    RoleMappingRule,
    UserRole,
)


def main():
    dialect = mysql.dialect()
    models = (
        OrganizationState,
        UserRole,
        RoleMappingRule,
        AuditLog,
        DenialCounter,
        PermissionAlert,
        PermissionEvent,
        AuthSession,
    )

    for model in models:
        table = model.__table__
        print(f"{CreateTable(table).compile(dialect=dialect)};")
        for index in sorted(table.indexes, key=lambda item: item.name):
            print(f"{CreateIndex(index).compile(dialect=dialect)};")

    print("""
INSERT INTO organization_state
    (id, version, dirty, synced_at, snapshot)
VALUES
    (1, 1, TRUE, 0, JSON_OBJECT());
""")


if __name__ == "__main__":
    main()