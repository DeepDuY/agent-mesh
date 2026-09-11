from agent_mesh.orchestrator.store.sqlite.agents import AgentMixin
from agent_mesh.orchestrator.store.sqlite.artifacts import ArtifactMixin
from agent_mesh.orchestrator.store.sqlite.files import FileMixin
from agent_mesh.orchestrator.store.sqlite.logs import TaskLogMixin
from agent_mesh.orchestrator.store.sqlite.settings import SettingsMixin
from agent_mesh.orchestrator.store.sqlite.skills import SkillsMixin
from agent_mesh.orchestrator.store.sqlite.tasks import QueueMixin, TaskMixin
from agent_mesh.orchestrator.store.sqlite.templates import TemplateMixin
from agent_mesh.orchestrator.store.sqlite.users import UsersMixin


class SQLiteStore(AgentMixin, TaskMixin, QueueMixin, ArtifactMixin, SettingsMixin, UsersMixin, SkillsMixin, FileMixin, TaskLogMixin, TemplateMixin):
    pass
