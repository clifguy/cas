"""Independent personal skills; explicit roots and metadata outside skills/."""

from pathlib import Path

from package_safety import name, regular_path, tree


class PersonalTarget:
    def __init__(self, root):
        self.root = regular_path(root)
        self.skills = regular_path(self.root / "skills")
        self.state = regular_path(self.root / ".skill-install")

    def __fspath__(self):
        return str(self.root)

    def __str__(self):
        return str(self.root)

    def __truediv__(self, slot):
        if slot == "manifest.json":
            return regular_path(self.state / "manifest.json")
        name(slot)
        return regular_path(self.skills / slot)

    def mkdir(self, **kwargs):
        self.skills.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)


class PersonalLayouts:
    kinds = ("codex-personal", "claude-personal")

    @staticmethod
    def layout(root, target_kind):
        if target_kind not in PersonalLayouts.kinds:
            raise ValueError("unsupported personal target kind")
        if not Path(root).is_absolute():
            raise ValueError("explicit absolute user configuration root required")
        root = regular_path(root)
        if not root.is_dir():
            raise ValueError("explicit existing user configuration root required")
        target = PersonalTarget(root)
        for path in (target.skills, target.state):
            if path.exists() and not path.is_dir():
                raise ValueError("personal destination must be a directory")
        return root, target, target.state

    @staticmethod
    def observation(target):
        return tree(target.skills)

    @staticmethod
    def adoption_slots(paths, target_kind):
        if not isinstance(paths, list) or len(paths) != len(set(paths)):
            raise ValueError("adoption requires unique explicit paths")
        slots = set()
        for path in paths:
            if not isinstance(path, str) or not path.startswith("skills/"):
                raise ValueError("adoption requires skills/<name>")
            slot = path.removeprefix("skills/")
            name(slot)
            slots.add(slot)
        return slots

    @staticmethod
    def source(bundle, target_kind):
        return regular_path(bundle)

    @staticmethod
    def payload_root(target):
        return target.skills

    @staticmethod
    def allow_legacy(target_kind):
        return False
