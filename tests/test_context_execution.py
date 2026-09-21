"""Context-adapter parity and safety for real Azure and disposable Kubernetes."""
import copy
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delivery
import platform_services as platform


class ContextExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        for name in ("serviceaccounts.yaml", "prerequisites.yaml", "manifest.yaml"):
            (self.bundle / name).write_text("synthetic reviewed manifest\n")
        self.kubeconfig = self.root / "owned-kubeconfig"
        self.kubeconfig.write_text("synthetic isolated kubeconfig")
        self.target = {
            "environment": "pprd", "region": "uks", "slot": "aks01",
            "subscription_id": "00000000-0000-0000-0000-000000000003",
            "resource_group": "test-rg", "cluster_name": "test-aks01",
            "namespace": "platform-demo", "deployment": "platform-demo",
            "namespaces": ["platform-demo"],
        }
        self.receipt = {
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "source_commit": "a" * 40, "image": "example.test/app@sha256:" + "b" * 64,
            "target": self.target,
            "releases": [{
                "name": "envoy-gateway", "namespace": "platform-demo",
                "chart": "chart.tgz", "values": [], "timeout_seconds": 300,
            }],
        }

    def core(self, module):
        if module is platform:
            return module._apply_to_context(
                self.bundle, "c" * 64, kubeconfig=self.kubeconfig,
                context="kind-owned-aks01", run_directory=self.root / "runtime",
            )
        return module._deploy_to_context(
            self.bundle, "c" * 64, kubeconfig=self.kubeconfig,
            context="kind-owned-aks01",
        )

    def verify_name(self, module):
        return "verify" if module is platform else "verify_bundle"

    def test_azure_wrappers_gate_and_create_user_context_before_shared_core(self):
        for module, wrapper, core_name in (
            (platform, platform.apply, "_apply_to_context"),
            (delivery, delivery.deploy, "_deploy_to_context"),
        ):
            with self.subTest(module=module.__name__):
                events, saved = [], []

                def account(tenant, subscription):
                    events.append("account")
                    self.assertEqual(tenant, self.receipt["tenant_id"])
                    self.assertEqual(subscription, self.target["subscription_id"])

                def command(args, **kwargs):
                    if args[:3] == ["az", "aks", "get-credentials"]:
                        events.append("credentials")
                        self.assertNotIn("--admin", args)
                        self.assertEqual(args[args.index("--format") + 1], "exec")
                        self.assertEqual(args[args.index("--context") + 1], "test-aks01")
                        Path(args[args.index("--file") + 1]).write_text("user config")
                    elif args[:2] == ["kubelogin", "convert-kubeconfig"]:
                        events.append("login")
                        self.assertEqual(args[args.index("--login") + 1], "azurecli")
                    else:
                        self.fail("Unexpected wrapper command")

                def execute(bundle, expected, **kwargs):
                    events.append("core")
                    self.assertEqual(bundle, self.bundle)
                    self.assertEqual(expected, "c" * 64)
                    self.assertEqual(kwargs["context"], "test-aks01")
                    config = kwargs["kubeconfig"]
                    self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                    saved.append(config)
                    return self.receipt

                with patch.object(module, self.verify_name(module), return_value=self.receipt), \
                        patch.object(module, "account", side_effect=account), \
                        patch.object(module, "run", side_effect=command), \
                        patch.object(module, core_name, side_effect=execute) as core:
                    self.assertEqual(wrapper(self.bundle, "c" * 64, yes=True), self.receipt)
                    core.assert_called_once()
                self.assertEqual(events, ["account", "credentials", "login", "core"])
                self.assertFalse(saved[0].exists(), "Temporary user credentials must be removed")

    def test_azure_account_failure_never_reaches_credentials_or_shared_execution(self):
        for module, wrapper, core_name in (
            (platform, platform.apply, "_apply_to_context"),
            (delivery, delivery.deploy, "_deploy_to_context"),
        ):
            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module), return_value=self.receipt), \
                    patch.object(module, "account", side_effect=ValueError("wrong tenant")), \
                    patch.object(module, "run") as command, \
                    patch.object(module, core_name) as core:
                with self.assertRaisesRegex(ValueError, "wrong tenant"):
                    wrapper(self.bundle, "c" * 64, yes=True)
                command.assert_not_called()
                core.assert_not_called()

    def test_azure_noninteractive_approval_gate_remains_closed(self):
        for module, wrapper in ((platform, platform.apply), (delivery, delivery.deploy)):
            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module), return_value=self.receipt), \
                    patch.object(module.sys.stdin, "isatty", return_value=False), \
                    patch.object(module, "account") as account:
                with self.assertRaises(ValueError):
                    wrapper(self.bundle, "c" * 64)
                account.assert_not_called()

    def test_shared_execution_pins_all_commands_to_the_explicit_context(self):
        for module in (platform, delivery):
            calls = []

            def command(args, **kwargs):
                calls.append((args, kwargs))
                return "namespace/platform-demo" if "--ignore-not-found" in args else ""

            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module), return_value=self.receipt), \
                    patch.object(module, "account") as account, \
                    patch.object(module, "run", side_effect=command):
                self.assertEqual(self.core(module), self.receipt)
                account.assert_not_called()
            self.assertTrue(calls)
            for args, kwargs in calls:
                self.assertIn(args[0], ("kubectl", "helm"))
                option = "--kube-context" if args[0] == "helm" else "--context"
                self.assertEqual(args[args.index(option) + 1], "kind-owned-aks01")
                self.assertEqual(args[args.index("--kubeconfig") + 1], str(self.kubeconfig))
                self.assertEqual(kwargs["env"]["KUBECONFIG"], str(self.kubeconfig))
                if args[0] == "helm":
                    self.assertEqual(kwargs["env"]["HELM_CONFIG_HOME"],
                                     str(self.root / "runtime/helm/config"))

    def test_untrusted_receipt_fails_before_any_cluster_operation(self):
        for module in (platform, delivery):
            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module), side_effect=ValueError("receipt changed")), \
                    patch.object(module, "run") as command:
                with self.assertRaisesRegex(ValueError, "receipt changed"):
                    self.core(module)
                command.assert_not_called()

    def test_receipt_rechecked_between_server_validation_and_mutation(self):
        for module in (platform, delivery):
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                return "namespace/platform-demo" if "--ignore-not-found" in args else ""

            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module),
                                 side_effect=[self.receipt, self.receipt, ValueError("bundle changed")]), \
                    patch.object(module, "run", side_effect=command):
                with self.assertRaisesRegex(ValueError, "bundle changed"):
                    self.core(module)
            self.assertTrue(any("--dry-run=server" in args for args in calls))
            self.assertFalse(any("apply" in args and "--dry-run=server" not in args for args in calls))

    def test_failed_server_validation_does_not_mutate_through_shared_core(self):
        for module in (platform, delivery):
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                if "--dry-run=server" in args:
                    raise subprocess.CalledProcessError(1, args)
                return "namespace/platform-demo" if "--ignore-not-found" in args else ""

            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module), return_value=self.receipt), \
                    patch.object(module, "run", side_effect=command):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.core(module)
            self.assertFalse(any("apply" in args and "--dry-run=server" not in args for args in calls))

    def test_shared_application_execution_keeps_service_and_ingress_verification(self):
        receipt = copy.deepcopy(self.receipt)
        receipt["target"]["verification"] = {"ingress": {"ca_file": "trust/ca.pem"}}
        with patch.object(delivery, "verify_bundle", return_value=receipt), \
                patch.object(delivery, "run") as command, \
                patch.object(delivery, "verify_service") as service, \
                patch.object(delivery, "verify_ingress") as ingress:
            self.core(delivery)
        service.assert_called_once()
        ingress.assert_called_once()
        for verification in (service, ingress):
            base = verification.call_args.args[0]
            self.assertEqual(base[base.index("--context") + 1], "kind-owned-aks01")
        self.assertEqual(ingress.call_args.args[-1], self.bundle / "ingress-ca.pem")
        self.assertTrue(any("rollout" in call.args[0] for call in command.call_args_list))

    def test_shared_core_requires_explicit_context_and_existing_kubeconfig(self):
        for module in (platform, delivery):
            function = module._apply_to_context if module is platform else module._deploy_to_context
            extra = {"run_directory": self.root / "runtime"} if module is platform else {}
            with self.subTest(module=module.__name__), \
                    patch.object(module, self.verify_name(module), return_value=self.receipt), \
                    patch.object(module, "run") as command:
                for context in ("", "two contexts", "-implicit"):
                    with self.assertRaisesRegex(ValueError, "explicit Kubernetes context"):
                        function(self.bundle, "c" * 64, kubeconfig=self.kubeconfig,
                                 context=context, **extra)
                with self.assertRaisesRegex(ValueError, "isolated kubeconfig"):
                    function(self.bundle, "c" * 64, kubeconfig=self.root / "missing",
                             context="kind-owned-aks01", **extra)
                command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
