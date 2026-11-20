"""The Writer: a three-agent CrewAI crew (analyst -> writer -> reviewer) on Amazon Bedrock."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import yaml

from ..config import WriterConfig

HERE = Path(__file__).parent
FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.DOTALL)


class BriefingWriter(Protocol):
    def write(self, inputs: dict[str, str]) -> str: ...


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = FENCE_RE.match(text)
    return m.group(1).strip() if m else text


# Bedrock models whose Converse API rejects the `stopSequences` field. CrewAI only knows
# about the OpenAI ones, so for these we tell it not to send stop sequences; it then
# trims the output at the stop words itself.
NO_STOP_SEQUENCE_MODELS = ("google.gemma",)


def _build_llm(model: str, temperature: float):
    from crewai import LLM

    llm = LLM(model=model, temperature=temperature)
    if any(marker in model.lower() for marker in NO_STOP_SEQUENCE_MODELS):
        # Instance-level override (the LLM is a pydantic model, hence object.__setattr__).
        object.__setattr__(llm, "supports_stop_words", lambda: False)
    return llm


class CrewWriter:
    """Builds a fresh crew per call (crews keep per-run state)."""

    def __init__(self, cfg: WriterConfig, llm_model: str):
        self.cfg = cfg
        self.llm_model = llm_model
        self.agents_cfg = yaml.safe_load((HERE / "agents.yaml").read_text())
        self.tasks_cfg = yaml.safe_load((HERE / "tasks.yaml").read_text())

    def _crew(self):
        from crewai import Agent, Crew, Process, Task

        main_llm = _build_llm(self.llm_model, self.cfg.temperature)
        analyst_llm = (
            _build_llm(self.cfg.analyst_model, self.cfg.temperature)
            if self.cfg.analyst_model
            else main_llm
        )
        llm_for = {"analyst": analyst_llm, "writer": main_llm, "reviewer": main_llm}

        agents = {
            name: Agent(
                **spec,
                llm=llm_for[name],
                allow_delegation=False,
                verbose=False,
                max_iter=3,
            )
            for name, spec in self.agents_cfg.items()
        }

        tasks: dict[str, Task] = {}
        for name, spec in self.tasks_cfg.items():
            tasks[name] = Task(
                description=spec["description"],
                expected_output=spec["expected_output"],
                agent=agents[spec["agent"]],
                context=[tasks[c] for c in spec.get("context", [])],
            )

        return Crew(
            agents=list(agents.values()),
            tasks=list(tasks.values()),
            process=Process.sequential,
            verbose=False,
        )

    def write(self, inputs: dict[str, str]) -> str:
        inputs = {"feedback": "none", **inputs}
        result = self._crew().kickoff(inputs=inputs)
        return _strip_fences(str(getattr(result, "raw", result)))
