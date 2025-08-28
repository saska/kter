"""
A kubernetes control panel.
"""

import json

from rich.pretty import pprint
from typing import Any
from textual.coordinate import Coordinate
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Header, Footer, OptionList, Pretty, Static
from textual.widgets.option_list import Option
from textual.screen import ModalScreen, Screen
from textual.widgets.data_table import ColumnDoesNotExist, CellDoesNotExist

from textual.containers import VerticalScroll

from kubernetes import client, config
from kubernetes.client.models.v1_pod import V1Pod
from kubernetes.client import ApiException


class KubeInterface:
    def __init__(self):
        config.load_kube_config()
        self.api = client.CoreV1Api()

    def get_pods(self, namespace=None):
        if namespace is None:
            return self.api.list_pod_for_all_namespaces(watch=False).items
        else:
            return self.api.list_namespaced_pod(namespace).items

    def get_namespaces(self):
        return [n.metadata.name for n in self.api.list_namespace(watch=False).items]

    def get_pod(self, name: str, namespace: str) -> V1Pod:
        return self.api.read_namespaced_pod(name, namespace)

    def get_pod_logs(self, name: str, namespace: str, container=None):
        return self.api.read_namespaced_pod_log(name, namespace, container=container)

    def get_contexts(self) -> list[dict[str, str]]:
        # As far as I can tell just returns a 2-tuple of (all contexts, current context)
        return config.list_kube_config_contexts()[0]

    def set_context(self, context: str) -> None:
        config.load_config(context=context)
        self.api = client.CoreV1Api()


class NamespaceSelectScreen(ModalScreen[str]):
    """Screen with a dialog select your namespace."""

    ALL_NAMESPACES_IDENTIFIER = "all"

    BINDINGS = [("escape", "app.pop_screen", "Exit")]

    def __init__(self, kube: KubeInterface, *args, **kwargs):
        self.kube = kube
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield OptionList(self.ALL_NAMESPACES_IDENTIFIER, *self.kube.get_namespaces())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        prompt = event.option._prompt
        if prompt == self.ALL_NAMESPACES_IDENTIFIER:
            self.dismiss(None)
        self.dismiss(prompt)


class KubeContextSelectScreen(ModalScreen[str]):
    """Screen with a dialog select your kube context."""

    BINDINGS = [("escape", "app.pop_screen", "Exit")]

    def __init__(self, kube: KubeInterface, *args, **kwargs):
        self.kube = kube
        self.contexts = {}
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        contexts = self.kube.get_contexts()
        yield OptionList(*[c["name"] for c in contexts])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        prompt = event.option._prompt
        self.dismiss(prompt)


class PodSummaryScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Exit")]

    def __init__(
        self, pod_name: str, pod_namespace: str, kube: KubeInterface, *args, **kwargs
    ):
        self.pod_name = pod_name
        self.pod_namespace = pod_namespace
        self.kube = kube
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Pretty(self.kube.get_pod(self.pod_name, self.pod_namespace).to_dict())
        )
        yield Footer()


class PodLogScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Exit")]

    def __init__(
        self,
        pod_name: str,
        pod_namespace: str,
        kube: KubeInterface,
        *args,
        container_name=None,
        **kwargs,
    ):
        self.pod_name = pod_name
        self.pod_namespace = pod_namespace
        self.kube = kube
        self.container_name = container_name
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:

        try:
            scroll = VerticalScroll(
                Static(
                    self.kube.get_pod_logs(
                        self.pod_name, self.pod_namespace, self.container_name
                    ),
                    markup=False,
                )
            )
            scroll.anchor()
            yield scroll
            yield Footer()
        except ApiException as e:
            # there's some weird loading going on here but apparently this exists
            app.notify(
                title=f"{e.status} {e.reason}",
                message=e.body,
                severity="error",
            )
            self.dismiss()


class PodLogContainerSelectScreen(ModalScreen[str | None]):

    BINDINGS = [("escape", "app.pop_screen", "Exit")]

    def __init__(
        self, pod_name: str, pod_namespace: str, kube: KubeInterface, *args, **kwargs
    ):
        self.pod_name = pod_name
        self.pod_namespace = pod_namespace
        self.kube = kube
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        try:
            pod = self.kube.get_pod(self.pod_name, self.pod_namespace)
        except ApiException as e:
            app.notify(title=f"{e.status} {e.reason}", message=e.body, severity="error")
            app.pop_screen()  # don't trigger dismiss stuff
        if len(pod.spec.containers) == 1:
            self.dismiss()
        else:
            yield OptionList(*[c.name for c in pod.spec.containers])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        prompt = event.option._prompt
        self.dismiss(prompt)


class PodTable(DataTable):
    def __init__(self, kube: KubeInterface, *args, **kwargs):
        self.kube = kube
        super().__init__(*args, **kwargs)

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.update_pods()

    def update_pods(self, namespace: str | None = None) -> None:
        pods = [
            self.pod_item(pod, include_namespace=(namespace is None))
            for pod in self.kube.get_pods(namespace=namespace)
        ]
        pod_table_key_list = [
            "namespace",
            "name",
            "ready",
            "status",
        ]
        selected = self.cursor_row
        for k in pod_table_key_list:
            try:
                self.remove_column(k)
            except ColumnDoesNotExist:
                pass
        if namespace is not None:
            pod_table_key_list.pop(0)
        for p in pod_table_key_list:
            self.add_column(p, key=p)
        self.clear()
        self.add_rows(pods)
        self.sort(*pod_table_key_list)
        selected = min(selected, len(self.rows))
        # TODO do a name search for this so it's 1. the same pod 2. if that doesn't exist the old cursor index 3. if that doesn't exist the last row
        self.move_cursor(row=selected)

    def pod_readiness(self, pod):
        if pod.status.container_statuses is None:
            return "0/0"
        container_count = len(pod.status.container_statuses)
        ready_count = len([p for p in pod.status.container_statuses if p.ready])
        return f"{ready_count}/{container_count}"

    def pod_item(self, pod, include_namespace: bool = True) -> list[str]:
        item: list[str] = [
            pod.metadata.namespace,
            pod.metadata.name,
            self.pod_readiness(pod),
            pod.status.phase,
        ]
        if not include_namespace:
            item.pop(0)
        return item


class KTer(App):
    CSS = """
    Screen { align: center middle; }
    PodTable { width: auto; }
    """

    BINDINGS = [
        Binding(
            "c",
            "select_context",
            "context",
            tooltip="Select your kubernetes context",
        ),
        Binding(
            "n",
            "select_namespace",
            "namespace",
            tooltip="Select namespace",
        ),
        Binding(
            "l",
            "logs",
            "Logs",
            tooltip="Logs",
        ),
    ]

    SUB_TITLE = "the kubernetes checker-helper"

    def __init__(self, *args, **kwargs):
        self.kube = KubeInterface()
        self.namespace = None
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        self.header = Header()
        self.pod_table = PodTable(self.kube)
        yield self.header
        yield self.pod_table
        yield Footer()

    def _update(self) -> None:
        self.pod_table.update_pods(namespace=self.namespace)

    def on_ready(self) -> None:
        self._update()
        self.set_interval(1, self._update)

    def action_select_namespace(self) -> None:
        def set_namespace(ns: str | None) -> None:
            self.namespace = ns
            self._update()

        self.push_screen(NamespaceSelectScreen(self.kube), set_namespace)

    def action_select_context(self) -> None:
        def set_context(context: str | None) -> None:
            self.kube.set_context(context)
            self._update()

        self.push_screen(KubeContextSelectScreen(self.kube), set_context)

    def action_logs(self) -> None:
        row_i = self.pod_table.cursor_row
        try:
            ns = self.pod_table.get_cell_at(
                Coordinate(
                    row=row_i, column=self.pod_table.get_column_index("namespace")
                )
            )
        except ColumnDoesNotExist:
            ns = self.namespace
        name = self.pod_table.get_cell_at(
            Coordinate(row=row_i, column=self.pod_table.get_column_index("name"))
        )

        def push_log_screen(container_name: str | None):
            self.push_screen(
                PodLogScreen(name, ns, self.kube, container_name=container_name)
            )

        self.push_screen(
            PodLogContainerSelectScreen(name, ns, self.kube), push_log_screen
        )

    def on_data_table_row_selected(self, event: PodTable.RowSelected) -> None:
        name = event.data_table.get_cell_at(
            event.data_table.get_cell_coordinate(event.row_key, "name")
        )
        try:
            ns = event.data_table.get_cell_at(
                event.data_table.get_cell_coordinate(event.row_key, "namespace")
            )
        except CellDoesNotExist:
            ns = self.namespace
        self.push_screen(PodSummaryScreen(name, ns, self.kube))


if __name__ == "__main__":
    app = KTer()
    app.run()
