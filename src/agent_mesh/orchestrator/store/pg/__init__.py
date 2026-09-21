from agent_mesh.orchestrator.store.pg.agents import AgentMixin
from agent_mesh.orchestrator.store.pg.artifacts import ArtifactMixin
from agent_mesh.orchestrator.store.pg.files import FileMixin
from agent_mesh.orchestrator.store.pg.logs import TaskLogMixin
from agent_mesh.orchestrator.store.pg.settings import SettingsMixin
from agent_mesh.orchestrator.store.pg.schedules import ScheduleMixin
from agent_mesh.orchestrator.store.pg.skills import SkillsMixin
from agent_mesh.orchestrator.store.pg.tasks import QueueMixin, TaskMixin
from agent_mesh.orchestrator.store.pg.teams import TeamMixin
from agent_mesh.orchestrator.store.pg.templates import TemplateMixin
from agent_mesh.orchestrator.store.pg.users import UsersMixin


class PostgresStore(
    AgentMixin,
    TaskMixin,
    QueueMixin,
    ArtifactMixin,
    SettingsMixin,
    UsersMixin,
    SkillsMixin,
    FileMixin,
    TaskLogMixin,
    TemplateMixin,
    TeamMixin,
    ScheduleMixin,
):
    pass
