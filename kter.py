"""
A kubernetes control panel.
"""

import asyncio
import re

from textual.coordinate import Coordinate
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Header, Footer, OptionList, Pretty, Static, Input
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

    async def get_pods(self, namespace=None):
        if namespace is None:
            pods = await asyncio.to_thread(self.api.list_pod_for_all_namespaces)
            return pods.items
        else:
            pods = await asyncio.to_thread(
                self.api.list_namespaced_pod,
                namespace,
            )
            return pods.items

    def get_namespaces(self):
        return [n.metadata.name for n in self.api.list_namespace(watch=False).items]

    def get_pod(self, name: str, namespace: str) -> V1Pod:
        return self.api.read_namespaced_pod(name, namespace)

    async def get_pod_logs(self, name: str, namespace: str, container=None):
        return await asyncio.to_thread(
            self.api.read_namespaced_pod_log, name, namespace, container=container
        )

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


class StaticPodLogViewer(Static):
    # TODO add streaming
    def __init__(
        self,
        pod_name: str,
        pod_namespace: str,
        kube: KubeInterface,
        *args,
        container_name: str | None = None,
        **kwargs,
    ):
        self.pod_name = pod_name
        self.pod_namespace = pod_namespace
        self.kube = kube
        self.container_name = container_name
        self.log_ = ""
        self.regex = None
        super().__init__(
            *args,
            markup=False,
            **kwargs,
        )

    async def on_mount(self):
        try:
            self.log_ = await self.kube.get_pod_logs(
                self.pod_name, self.pod_namespace, container=self.container_name
            )
        except ApiException as e:
            # there's some weird loading going on here but apparently this exists
            # also ugly to do it from the widget instead of the screen I guess
            # but whatchagonnado, fix it?
            app.notify(
                title=f"{e.status} {e.reason}",
                message=e.body,
                severity="error",
            )
            app.pop_screen()

        self.update(content=self.log_)

    def update_with_regex(self, regex):
        self.regex = regex
        content_lines = [
            line for line in self.log_.split("\n") if re.search(regex, line)
        ]
        self.update(content="\n".join(content_lines))

    def update_clear_regex(self):
        self.regex = None
        self.update(content=self.log_)

    async def refresh_logs(self):
        self.log_ = await self.kube.get_pod_logs(
            self.pod_name, self.pod_namespace, container=self.container_name
        )
        if self.regex is not None:
            self.update_with_regex(self.regex)
        else:
            self.update_clear_regex()


class PodLogRegexFilterScreen(ModalScreen[str | None]):

    BINDINGS = [("escape", "app.pop_screen", "Exit")]

    def compose(self) -> ComposeResult:
        yield Input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class PodLogScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Exit"),
        ("r", "refresh_logs", "refresh"),
        ("g", "regex_filter", "regex"),
        ("ctrl+g", "clear_regex", "clear regex"),
    ]

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
        self.static = None
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        self.static = StaticPodLogViewer(
            self.pod_name,
            self.pod_namespace,
            self.kube,
            container_name=self.container_name,
        )
        scroll = VerticalScroll(self.static)
        scroll.anchor()
        yield scroll
        yield Footer()

    def action_regex_filter(self):
        def regex_filter(regex: str | None) -> None:
            self.static.update_with_regex(regex)

        app.push_screen(PodLogRegexFilterScreen(), regex_filter)

    def action_clear_regex(self):
        self.static.update_clear_regex()

    async def action_refresh_logs(self):
        await self.static.refresh_logs()


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
        self.previously_namespaced = False
        super().__init__(*args, **kwargs)

    async def on_mount(self) -> None:
        self.cursor_type = "row"
        await self.update_pods()

    async def update_pods(self, namespace: str | None = None) -> None:
        """Update the pods in the pod table.

        The reason this is so convoluted instead of emptying it out and just
        re-drawing the entire thing is that if you have a vertical scrollbar
        and are not at the top of the list it bounces around annoyingly on each
        update so we do it the hard way.
        """
        pods = await self.kube.get_pods(namespace=namespace)

        pod_table_key_list = [
            "namespace",
            "name",
            "ready",
            "status",
        ]

        # Have to force complete redraw if we want to re-add
        # namespace to the table as I can't find a clear way
        # to add a column to the left side of the table
        if namespace is not None:
            self.previously_namespaced = True
        if namespace is None and self.previously_namespaced:
            self.clear()
            for key in pod_table_key_list:
                try:
                    self.remove_column(key)
                except ColumnDoesNotExist:
                    pass
            self.previously_namespaced = False

        new_rows = {
            f"{p.metadata.namespace}.{p.metadata.name}": self.pod_item(
                p, include_namespace=(namespace is None)
            )
            for p in pods
        }

        selected = self.cursor_row

        if namespace is not None:
            pod_table_key_list.pop(0)
            try:
                self.remove_column("namespace")
            except ColumnDoesNotExist:
                pass

        for p in pod_table_key_list:
            try:
                # get index since get_column returns an iterator
                self.get_column_index(p)
            except ColumnDoesNotExist:
                self.add_column(p, key=p)

        deletes = []
        for row in self.rows:
            if row.value in new_rows:
                *_, ready, status = new_rows.pop(row.value)
                self.update_cell(row, "ready", ready)
                self.update_cell(row, "status", status)
            else:
                deletes.append(row)

        for d in deletes:
            self.remove_row(d)

        for key, row in new_rows.items():
            self.add_row(*row, key=key)
        self.sort(*pod_table_key_list)
        selected = min(selected, len(self.rows))
        # TODO do a name search for this so it's 1. the same pod 2. if that doesn't exist the old cursor index 3. if that doesn't exist the last row
        # Easy if this gets merged https://github.com/Textualize/textual/pull/6081
        self.move_cursor(row=selected)

    def pod_readiness(self, pod):
        if pod.status.container_statuses is None:
            return "0/0"
        container_count = len(pod.status.container_statuses)
        ready_count = len([p for p in pod.status.container_statuses if p.ready])
        return f"{ready_count}/{container_count}"

    def pod_status(self, pod):
        container_statuses = pod.status.container_statuses
        for c in container_statuses:
            if c.state.waiting is not None:
                return c.state.waiting.reason
        return pod.status.phase

    def pod_item(self, pod, include_namespace: bool = True) -> list[str]:
        item: list[str] = [
            pod.metadata.namespace,
            pod.metadata.name,
            self.pod_readiness(pod),
            self.pod_status(pod),
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

    async def _update(self) -> None:
        await self.pod_table.update_pods(namespace=self.namespace)

    async def on_ready(self) -> None:
        await self._update()
        self.set_interval(1, self._update)

    def action_select_namespace(self) -> None:
        async def set_namespace(ns: str | None) -> None:
            self.namespace = ns
            await self._update()

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
