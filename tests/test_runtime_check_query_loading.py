"""Startup access checks must not traverse the project's eager world graph."""
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine, event
from sqlalchemy.orm import Session, declarative_base, relationship
import pytest

from src.bootstrap.runtime_checks import _readiness_query_without_relationships


@pytest.mark.parametrize("reader", ["runtime-readiness", "db-bootstrap-status"])
def test_readiness_query_reads_only_scalar_row_without_joined_or_selectin_relationships(reader):
    base = declarative_base()
    class AccessProject(base):
        __tablename__ = "audit_projects"
        id = Column(Integer, primary_key=True)
        label = Column(String)
        worlds = relationship("AccessWorld", lazy="selectin")
        roles = relationship("AccessRole", lazy="joined")
    class AccessWorld(base):
        __tablename__ = "audit_worlds"
        id = Column(Integer, primary_key=True)
        project_id = Column(Integer, ForeignKey("audit_projects.id"))
    class AccessRole(base):
        __tablename__ = "audit_roles"
        id = Column(Integer, primary_key=True)
        project_id = Column(Integer, ForeignKey("audit_projects.id"))
    engine = create_engine("sqlite:///:memory:")
    try:
        base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(AccessProject(id=1, label="scalar readiness fields",
                                     worlds=[AccessWorld() for _ in range(8)], roles=[AccessRole()]))
            session.commit()
        statements = []
        def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", record_statement)
        with Session(engine) as session:
            if reader == "runtime-readiness":
                row = _readiness_query_without_relationships(session.query(AccessProject)).filter_by(id=1).one()
            else:
                from src.bootstrap.db_bootstrap import _query_first_by_fields
                row = _query_first_by_fields(session, AccessProject, id=1)
            assert row.label == "scalar readiness fields"
            assert row.worlds == [] and row.roles == []
            assert not session.dirty and not session.new
        assert len(statements) == 1
        assert "JOIN" not in statements[0]
        assert "audit_worlds" not in statements[0]
    finally:
        engine.dispose()
