import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data.models import (
    Base,
    OrganizationMemberModel,
    OrganizationModel,
    UserResultProfileModel,
    UserModel,
)
from app.data.bootstrap import ensure_schema_compatibility, seed_defaults
from app.core.config import Settings, settings, validate_runtime_settings
from app.g2b.opening_results import models as opening_result_models  # noqa: F401
from app.g2b.opening_results.models import SheetDestinationModel
from app.routers.auth import google_login, login, single_user_session
from app.schemas import GoogleLoginRequest, LoginRequest
from app.services.auth_service import (
    authenticate_google_user,
    create_access_token,
    parse_access_token,
    require_auth,
    require_organization_auth,
    hash_password,
    verify_cloud_scheduler_oidc_token,
    verify_password,
)


class AuthTenancyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.organization = OrganizationModel(name="테스트 조직", slug="auth-test")
        self.user = UserModel(
            username="auth-user",
            password_salt="salt",
            password_hash="hash",
            email="auth-user@iceu.kr",
            role="admin",
            is_active=True,
        )
        self.db.add_all([self.organization, self.user])
        self.db.flush()
        self.membership = OrganizationMemberModel(
            organization_id=self.organization.id,
            user_id=self.user.id,
            role="admin",
            is_active=True,
        )
        self.db.add(self.membership)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_seed_defaults_disables_legacy_organization_sheet_destinations(self):
        destination = SheetDestinationModel(
            organization_id=self.organization.id,
            owner_user_id=None,
            label="기존 조직 공용 Sheet",
            spreadsheet_id="legacy-organization-sheet",
            tab_name="개찰결과",
            is_default=True,
            is_active=True,
        )
        self.db.add(destination)
        self.db.commit()

        seed_defaults(self.db)

        self.db.refresh(destination)
        self.assertFalse(destination.is_active)
        self.assertFalse(destination.is_default)

    def make_token(self) -> str:
        return create_access_token(
            user_id=self.user.id,
            username=self.user.username,
            role=self.user.role,
        )

    @staticmethod
    def loopback_request():
        return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    def test_versioned_token_has_expiry_and_resolves_organization_from_database(self):
        token = self.make_token()
        payload = parse_access_token(token)
        auth = require_auth(authorization=f"Bearer {token}", db=self.db)
        organization_auth = require_organization_auth(auth=auth, db=self.db)

        self.assertEqual(payload["ver"], 2)
        self.assertGreater(payload["exp"], payload["iat"])
        self.assertEqual(organization_auth["user_id"], self.user.id)
        self.assertEqual(organization_auth["organization_id"], self.organization.id)
        self.assertEqual(organization_auth["organization_role"], "admin")

    def test_expired_access_token_is_rejected(self):
        with patch.object(settings, "auth_token_ttl_hours", -1):
            token = self.make_token()

        with self.assertRaises(ValueError):
            parse_access_token(token)

        with self.assertRaises(HTTPException) as raised:
            require_auth(authorization=f"Bearer {token}", db=self.db)
        self.assertEqual(raised.exception.status_code, 401)

    def test_existing_token_is_rejected_immediately_after_user_is_disabled(self):
        token = self.make_token()
        self.user.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            require_auth(authorization=f"Bearer {token}", db=self.db)

        self.assertEqual(raised.exception.status_code, 401)

    def test_existing_token_loses_organization_access_after_membership_is_disabled(self):
        token = self.make_token()
        auth = require_auth(authorization=f"Bearer {token}", db=self.db)
        self.membership.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            require_organization_auth(auth=auth, db=self.db)

        self.assertEqual(raised.exception.status_code, 403)

    def test_google_login_verifies_identity_and_binds_registered_user(self):
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "google-client-id",
            "sub": "google-user-123",
            "email": "AUTH-USER@iceu.kr",
            "email_verified": True,
            "hd": "iceu.kr",
            "name": "인증 사용자",
        }
        with (
            patch.object(settings, "google_oauth_client_id", "google-client-id"),
            patch.object(
                settings,
                "allowed_login_domains",
                ("iceu.kr", "iceu.co.kr"),
            ),
            patch(
                "app.services.auth_service.google_id_token.verify_oauth2_token",
                return_value=claims,
            ),
        ):
            response = google_login(
                GoogleLoginRequest(credential="signed-google-id-token"),
                db=self.db,
            )

        self.db.refresh(self.user)
        self.assertEqual(response.user_id, self.user.id)
        self.assertEqual(response.email, "auth-user@iceu.kr")
        self.assertEqual(response.display_name, "인증 사용자")
        self.assertEqual(self.user.google_sub, "google-user-123")
        self.assertIsNotNone(self.user.last_login_at)
        self.assertEqual(parse_access_token(response.access_token)["sub"], str(self.user.id))

    def test_first_company_domain_login_creates_user_membership_and_empty_profile_once(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        email = "new-user@iceu.co.kr"
        claims = {
            "iss": "accounts.google.com",
            "aud": "google-client-id",
            "sub": "new-google-user-456",
            "email": email,
            "email_verified": True,
            "hd": "iceu.co.kr",
            "name": "신규 사용자",
        }
        with Session(engine) as db:
            seed_defaults(db)
            with (
                patch.object(settings, "google_oauth_client_id", "google-client-id"),
                patch.object(
                    settings,
                    "allowed_login_domains",
                    ("iceu.kr", "iceu.co.kr"),
                ),
                patch(
                    "app.services.auth_service.google_id_token.verify_oauth2_token",
                    return_value=claims,
                ),
            ):
                first_response = google_login(
                    GoogleLoginRequest(credential="signed-google-id-token"),
                    db=db,
                )
                second_response = google_login(
                    GoogleLoginRequest(credential="signed-google-id-token"),
                    db=db,
                )

            user = db.scalar(select(UserModel).where(UserModel.email == email))
            membership = db.scalar(
                select(OrganizationMemberModel).where(
                    OrganizationMemberModel.user_id == user.id
                )
            )
            profile = db.scalar(
                select(UserResultProfileModel).where(
                    UserResultProfileModel.user_id == user.id
                )
            )

            self.assertEqual(first_response.user_id, user.id)
            self.assertEqual(second_response.user_id, user.id)
            self.assertEqual(user.role, "viewer")
            self.assertTrue(membership.is_active)
            self.assertEqual(membership.role, "member")
            self.assertFalse(profile.enabled)
            self.assertEqual(profile.keywords, "")
            self.assertEqual(profile.excluded_keywords, "")
            self.assertEqual(
                db.scalar(
                    select(func.count(UserModel.id)).where(UserModel.email == email)
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count(OrganizationMemberModel.id)).where(
                        OrganizationMemberModel.user_id == user.id
                    )
                ),
                1,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count(UserResultProfileModel.id)).where(
                        UserResultProfileModel.user_id == user.id
                    )
                ),
                1,
            )
        engine.dispose()

    def test_new_iceu_kr_user_does_not_require_email_registration(self):
        user = authenticate_google_user(
            self.db,
            {
                "sub": "new-iceu-kr-user",
                "email": "new-user@iceu.kr",
                "display_name": "신규 사용자",
            },
        )

        self.assertEqual(user.email, "new-user@iceu.kr")
        self.assertEqual(user.role, "viewer")
        self.assertTrue(user.is_active)

    def test_google_login_rejects_external_missing_domain_and_unverified_email(self):
        base_claims = {
            "iss": "https://accounts.google.com",
            "aud": "google-client-id",
            "sub": "blocked-google-user",
            "email": "auth-user@iceu.kr",
            "email_verified": True,
            "hd": "iceu.kr",
            "name": "차단 대상",
        }
        cases = {
            "external-domain": {
                **base_claims,
                "email": "user@example.com",
                "hd": "example.com",
            },
            "missing-hosted-domain": {**base_claims, "hd": ""},
            "unverified-email": {**base_claims, "email_verified": False},
        }

        for name, claims in cases.items():
            with self.subTest(name=name):
                with (
                    patch.object(settings, "google_oauth_client_id", "google-client-id"),
                    patch.object(
                        settings,
                        "allowed_login_domains",
                        ("iceu.kr", "iceu.co.kr"),
                    ),
                    patch(
                        "app.services.auth_service.google_id_token.verify_oauth2_token",
                        return_value=claims,
                    ),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        google_login(
                            GoogleLoginRequest(credential="blocked-token"),
                            db=self.db,
                        )
                self.assertEqual(raised.exception.status_code, 403)

        self.db.refresh(self.user)
        self.assertIsNone(self.user.google_sub)

    def test_google_login_rejects_inactive_user(self):
        self.user.is_active = False
        self.db.commit()
        inactive_claims = {
            "iss": "accounts.google.com",
            "aud": "google-client-id",
            "sub": "inactive-google-user",
            "email": self.user.email,
            "email_verified": True,
            "hd": "iceu.kr",
            "name": "비활성 사용자",
        }
        with (
            patch.object(settings, "google_oauth_client_id", "google-client-id"),
            patch.object(
                settings,
                "allowed_login_domains",
                ("iceu.kr", "iceu.co.kr"),
            ),
            patch(
                "app.services.auth_service.google_id_token.verify_oauth2_token",
                return_value=inactive_claims,
            ),
        ):
            with self.assertRaises(HTTPException) as inactive:
                google_login(
                    GoogleLoginRequest(credential="inactive-token"),
                    db=self.db,
                )
        self.assertEqual(inactive.exception.status_code, 403)

    def test_google_login_rejects_invalid_token_and_missing_configuration(self):
        with (
            patch.object(settings, "google_oauth_client_id", "google-client-id"),
            patch.object(
                settings,
                "allowed_login_domains",
                ("iceu.kr", "iceu.co.kr"),
            ),
            patch(
                "app.services.auth_service.google_id_token.verify_oauth2_token",
                side_effect=ValueError("invalid signature"),
            ),
        ):
            with self.assertRaises(HTTPException) as invalid:
                google_login(
                    GoogleLoginRequest(credential="invalid-token"),
                    db=self.db,
                )
        self.assertEqual(invalid.exception.status_code, 401)

        with (
            patch.object(settings, "google_oauth_client_id", ""),
            patch.object(
                settings,
                "allowed_login_domains",
                ("iceu.kr", "iceu.co.kr"),
            ),
        ):
            with self.assertRaises(HTTPException) as unavailable:
                google_login(
                    GoogleLoginRequest(credential="any-token"),
                    db=self.db,
                )
        self.assertEqual(unavailable.exception.status_code, 503)

    def test_legacy_password_login_is_disabled_by_default(self):
        with patch.object(settings, "legacy_password_login_enabled", False):
            with self.assertRaises(HTTPException) as raised:
                login(
                    LoginRequest(username="auth-user", password="any-password"),
                    db=self.db,
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_single_user_session_is_disabled_by_default(self):
        with patch.object(settings, "single_user_mode_enabled", False):
            with self.assertRaises(HTTPException) as raised:
                single_user_session(request=self.loopback_request(), db=self.db)

        self.assertEqual(raised.exception.status_code, 403)

    def test_single_user_session_issues_default_user_token_when_enabled(self):
        with (
            patch.object(settings, "single_user_mode_enabled", True),
            patch.object(settings, "single_user_username", self.user.username),
        ):
            response = single_user_session(request=self.loopback_request(), db=self.db)

        self.assertEqual(response.user_id, self.user.id)
        self.assertEqual(response.organization_id, self.organization.id)
        self.assertEqual(parse_access_token(response.access_token)["sub"], str(self.user.id))

    def test_single_user_session_rejects_remote_clients(self):
        remote_request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"))
        with (
            patch.object(settings, "single_user_mode_enabled", True),
            patch.object(settings, "single_user_username", self.user.username),
        ):
            with self.assertRaises(HTTPException) as raised:
                single_user_session(request=remote_request, db=self.db)

        self.assertEqual(raised.exception.status_code, 403)

    def test_google_login_retries_once_after_first_login_unique_conflict(self):
        identity = {
            "sub": "google-race-user",
            "email": self.user.email,
            "hosted_domain": "iceu.kr",
            "display_name": "동시 로그인 사용자",
        }
        attempts = 0

        def resolve_after_conflict(db, received_identity):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise IntegrityError("INSERT users", {}, Exception("duplicate"))
            return authenticate_google_user(db, received_identity)

        with (
            patch("app.routers.auth.verify_google_identity", return_value=identity),
            patch(
                "app.routers.auth.authenticate_google_user",
                side_effect=resolve_after_conflict,
            ),
        ):
            response = google_login(
                GoogleLoginRequest(credential="concurrent-token"),
                db=self.db,
            )

        self.assertEqual(response.user_id, self.user.id)
        self.assertEqual(attempts, 2)

    def test_bootstrap_backfills_all_legacy_users_into_default_organization_once(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            legacy_user = UserModel(
                username="legacy-user",
                password_salt="salt",
                password_hash="hash",
                role="viewer",
                is_active=True,
            )
            db.add(legacy_user)
            db.commit()

            seed_defaults(db)
            seed_defaults(db)

            membership = db.scalar(
                select(OrganizationMemberModel).where(
                    OrganizationMemberModel.user_id == legacy_user.id
                )
            )
            self.assertIsNotNone(membership)
            self.assertEqual(membership.role, "member")
            self.assertEqual(
                db.scalar(
                    select(func.count(OrganizationMemberModel.id)).where(
                        OrganizationMemberModel.user_id == legacy_user.id
                    )
                ),
                1,
            )

            db.delete(membership)
            later_user = UserModel(
                username="later-unassigned-user",
                password_salt="salt",
                password_hash="hash",
                role="viewer",
                is_active=True,
            )
            db.add(later_user)
            db.commit()

            seed_defaults(db)

            self.assertIsNone(
                db.scalar(
                    select(OrganizationMemberModel).where(
                        OrganizationMemberModel.user_id == legacy_user.id
                    )
                )
            )
            self.assertIsNone(
                db.scalar(
                    select(OrganizationMemberModel).where(
                        OrganizationMemberModel.user_id == later_user.id
                    )
                )
            )
        engine.dispose()

    def test_bootstrap_copies_legacy_profile_once_and_new_member_starts_empty(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            legacy_user = UserModel(
                username="legacy-profile-user",
                password_salt="salt",
                password_hash="hash",
                role="viewer",
                is_active=True,
            )
            db.add(legacy_user)
            db.commit()

            seed_defaults(db)

            legacy_profile = db.scalar(
                select(UserResultProfileModel).where(
                    UserResultProfileModel.user_id == legacy_user.id
                )
            )
            organization = db.scalar(
                select(OrganizationModel).where(
                    OrganizationModel.slug == settings.default_organization_slug
                )
            )
            self.assertTrue(legacy_profile.enabled)
            self.assertEqual(legacy_profile.keywords, "클라우드,AI,교육")

            later_user = UserModel(
                username="later-profile-user",
                password_salt="salt",
                password_hash="hash",
                role="viewer",
                is_active=True,
            )
            db.add(later_user)
            db.flush()
            db.add(
                OrganizationMemberModel(
                    organization_id=organization.id,
                    user_id=later_user.id,
                    role="member",
                    is_active=True,
                )
            )
            db.commit()

            seed_defaults(db)
            seed_defaults(db)

            later_profile = db.scalar(
                select(UserResultProfileModel).where(
                    UserResultProfileModel.user_id == later_user.id
                )
            )
            self.assertFalse(later_profile.enabled)
            self.assertEqual(later_profile.keywords, "")
            self.assertEqual(later_profile.excluded_keywords, "")
            self.assertEqual(
                db.scalar(
                    select(func.count(UserResultProfileModel.id)).where(
                        UserResultProfileModel.user_id == later_user.id
                    )
                ),
                1,
            )
        engine.dispose()

    def test_production_rejects_public_auth_defaults(self):
        with self.assertRaises(RuntimeError) as raised:
            validate_runtime_settings(
                Settings(
                    environment="production",
                    auth_secret_key="change-me-in-production",
                    default_admin_password="icore1234!",
                )
            )

        self.assertIn("AUTH_SECRET_KEY", str(raised.exception))
        self.assertIn("DEFAULT_ADMIN_PASSWORD", str(raised.exception))

    def test_production_rejects_local_example_auth_secret(self):
        with self.assertRaises(RuntimeError) as raised:
            validate_runtime_settings(
                Settings(
                    environment="production",
                    auth_secret_key="local-only-change-me-at-least-32-characters",
                    default_admin_password="strong-admin-password",
                    google_oauth_client_id="google-client-id",
                    allowed_login_domains=("iceu.kr", "iceu.co.kr"),
                    cors_allowed_origins=("https://app.iceu.kr",),
                    scraper_internal_token="s" * 32,
                    g2b_award_service_key="award-service-key",
                    g2b_award_scheduler_target_url=(
                        "https://api.iceu.kr/api/v1/results/internal/collect"
                    ),
                    cloud_scheduler_invoker_service_account=(
                        "scheduler@project.iam.gserviceaccount.com"
                    ),
                )
            )

        self.assertIn("AUTH_SECRET_KEY", str(raised.exception))

    def test_production_accepts_explicit_strong_auth_secrets(self):
        validate_runtime_settings(
            Settings(
                environment="production",
                auth_secret_key="a" * 32,
                default_admin_password="strong-admin-password",
                google_oauth_client_id="google-client-id",
                allowed_login_domains=("iceu.kr", "iceu.co.kr"),
                cors_allowed_origins=("https://app.iceu.kr",),
                scraper_internal_token="s" * 32,
                g2b_award_service_key="award-service-key",
                g2b_award_scheduler_target_url=(
                    "https://api.iceu.kr/api/v1/results/internal/collect"
                ),
                cloud_scheduler_invoker_service_account=(
                    "scheduler@project.iam.gserviceaccount.com"
                ),
            )
        )

    def test_production_rejects_single_user_mode(self):
        with self.assertRaises(RuntimeError) as raised:
            validate_runtime_settings(
                Settings(
                    environment="production",
                    auth_secret_key="a" * 32,
                    default_admin_password="strong-admin-password",
                    google_oauth_client_id="google-client-id",
                    allowed_login_domains=("iceu.kr", "iceu.co.kr"),
                    cors_allowed_origins=("https://app.iceu.kr",),
                    scraper_internal_token="s" * 32,
                    g2b_award_service_key="award-service-key",
                    g2b_award_scheduler_target_url=(
                        "https://api.iceu.kr/api/v1/results/internal/collect"
                    ),
                    cloud_scheduler_invoker_service_account=(
                        "scheduler@project.iam.gserviceaccount.com"
                    ),
                    single_user_mode_enabled=True,
                )
            )

        self.assertIn("SINGLE_USER_MODE_ENABLED", str(raised.exception))

    def test_staging_rejects_single_user_mode(self):
        with self.assertRaises(RuntimeError) as raised:
            validate_runtime_settings(
                Settings(environment="staging", single_user_mode_enabled=True)
            )

        self.assertIn("SINGLE_USER_MODE_ENABLED", str(raised.exception))

    def test_production_rejects_incomplete_google_auth_settings(self):
        base = {
            "environment": "production",
            "auth_secret_key": "a" * 32,
            "default_admin_password": "strong-admin-password",
            "google_oauth_client_id": "google-client-id",
            "allowed_login_domains": ("iceu.kr", "iceu.co.kr"),
            "cors_allowed_origins": ("https://app.iceu.kr",),
            "scraper_internal_token": "s" * 32,
            "g2b_award_service_key": "award-service-key",
            "g2b_award_scheduler_target_url": (
                "https://api.iceu.kr/api/v1/results/internal/collect"
            ),
            "cloud_scheduler_invoker_service_account": (
                "scheduler@project.iam.gserviceaccount.com"
            ),
        }
        cases = {
            "missing-client-id": (
                {**base, "google_oauth_client_id": ""},
                "GOOGLE_OAUTH_CLIENT_ID",
            ),
            "missing-company-domain": (
                {**base, "allowed_login_domains": ("iceu.kr",)},
                "ALLOWED_LOGIN_DOMAINS",
            ),
            "external-domain": (
                {**base, "allowed_login_domains": ("iceu.kr", "example.com")},
                "ALLOWED_LOGIN_DOMAINS",
            ),
            "legacy-password": (
                {**base, "legacy_password_login_enabled": True},
                "LEGACY_PASSWORD_LOGIN_ENABLED",
            ),
            "wildcard-cors": (
                {**base, "cors_allowed_origins": ("*",)},
                "CORS_ALLOWED_ORIGINS",
            ),
        }

        for name, (values, expected_message) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError) as raised:
                    validate_runtime_settings(Settings(**values))
                self.assertIn(expected_message, str(raised.exception))

    def test_production_rejects_incomplete_opening_result_scheduler_settings(self):
        base = {
            "environment": "production",
            "auth_secret_key": "a" * 32,
            "default_admin_password": "strong-admin-password",
            "google_oauth_client_id": "google-client-id",
            "allowed_login_domains": ("iceu.kr", "iceu.co.kr"),
            "cors_allowed_origins": ("https://app.iceu.kr",),
            "scraper_internal_token": "s" * 32,
            "g2b_award_service_key": "award-service-key",
            "g2b_award_scheduler_target_url": (
                "https://api.iceu.kr/api/v1/results/internal/collect"
            ),
            "cloud_scheduler_invoker_service_account": (
                "scheduler@project.iam.gserviceaccount.com"
            ),
        }
        cases = {
            "short-internal-token": (
                {**base, "scraper_internal_token": "short"},
                "SCRAPER_INTERNAL_TOKEN",
            ),
            "missing-award-key": (
                {**base, "g2b_award_service_key": ""},
                "G2B_AWARD_SERVICE_KEY",
            ),
            "insecure-target": (
                {
                    **base,
                    "g2b_award_scheduler_target_url": (
                        "http://api.iceu.kr/api/v1/results/internal/collect"
                    ),
                },
                "G2B_AWARD_SCHEDULER_TARGET_URL",
            ),
            "wrong-target-path": (
                {
                    **base,
                    "g2b_award_scheduler_target_url": "https://api.iceu.kr/api/health",
                },
                "G2B_AWARD_SCHEDULER_TARGET_URL",
            ),
            "missing-invoker": (
                {**base, "cloud_scheduler_invoker_service_account": ""},
                "CLOUD_SCHEDULER_INVOKER_SERVICE_ACCOUNT",
            ),
        }

        for name, (values, expected_message) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError) as raised:
                    validate_runtime_settings(Settings(**values))
                self.assertIn(expected_message, str(raised.exception))

    def test_scheduler_oidc_is_skipped_only_in_local_or_test(self):
        for environment in ("local", "test"):
            with self.subTest(environment=environment):
                with patch.object(settings, "environment", environment):
                    verify_cloud_scheduler_oidc_token(authorization=None)

    def test_scheduler_oidc_accepts_expected_audience_and_service_account(self):
        audience = "https://api.iceu.kr/api/v1/results/internal/collect"
        service_account = "scheduler@project.iam.gserviceaccount.com"
        claims = {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "sub": "scheduler-service-account-subject",
            "email": service_account,
            "email_verified": True,
        }
        with (
            patch.object(settings, "environment", "production"),
            patch.object(settings, "g2b_award_scheduler_target_url", audience),
            patch.object(
                settings,
                "cloud_scheduler_invoker_service_account",
                service_account,
            ),
            patch(
                "app.services.auth_service.google_id_token.verify_oauth2_token",
                return_value=claims,
            ) as verifier,
        ):
            verify_cloud_scheduler_oidc_token(authorization="Bearer signed-oidc-token")

        verifier.assert_called_once()
        self.assertEqual(verifier.call_args.args[2], audience)

    def test_scheduler_oidc_uses_explicit_cloud_run_audience(self):
        target_url = "https://api.iceu.kr/api/v1/results/internal/collect"
        audience = "https://api.iceu.kr"
        service_account = "scheduler@project.iam.gserviceaccount.com"
        claims = {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "sub": "scheduler-service-account-subject",
            "email": service_account,
            "email_verified": True,
        }
        with (
            patch.object(settings, "environment", "production"),
            patch.object(settings, "g2b_award_scheduler_target_url", target_url),
            patch.object(
                settings,
                "g2b_award_scheduler_oidc_audience",
                audience,
            ),
            patch.object(
                settings,
                "cloud_scheduler_invoker_service_account",
                service_account,
            ),
            patch(
                "app.services.auth_service.google_id_token.verify_oauth2_token",
                return_value=claims,
            ) as verifier,
        ):
            verify_cloud_scheduler_oidc_token(authorization="Bearer signed-oidc-token")

        verifier.assert_called_once()
        self.assertEqual(verifier.call_args.args[2], audience)

    def test_scheduler_oidc_rejects_missing_or_wrong_identity(self):
        audience = "https://api.iceu.kr/api/v1/results/internal/collect"
        service_account = "scheduler@project.iam.gserviceaccount.com"
        with (
            patch.object(settings, "environment", "production"),
            patch.object(settings, "g2b_award_scheduler_target_url", audience),
            patch.object(
                settings,
                "cloud_scheduler_invoker_service_account",
                service_account,
            ),
        ):
            with self.assertRaises(HTTPException) as missing:
                verify_cloud_scheduler_oidc_token(authorization=None)
            self.assertEqual(missing.exception.status_code, 401)

            wrong_claims = {
                "iss": "https://accounts.google.com",
                "aud": audience,
                "sub": "wrong-service-account-subject",
                "email": "other@project.iam.gserviceaccount.com",
                "email_verified": True,
            }
            with patch(
                "app.services.auth_service.google_id_token.verify_oauth2_token",
                return_value=wrong_claims,
            ):
                with self.assertRaises(HTTPException) as wrong_identity:
                    verify_cloud_scheduler_oidc_token(
                        authorization="Bearer signed-oidc-token"
                    )
            self.assertEqual(wrong_identity.exception.status_code, 401)

    def test_bootstrap_rotates_legacy_default_admin_password(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        old_salt, old_hash = hash_password("icore1234!")
        with Session(engine) as db:
            db.add(
                UserModel(
                    username=settings.default_admin_username,
                    password_salt=old_salt,
                    password_hash=old_hash,
                    role="admin",
                    is_active=True,
                )
            )
            db.commit()

            with patch.object(settings, "default_admin_password", "new-strong-admin-password"):
                seed_defaults(db)

            admin = db.scalar(
                select(UserModel).where(
                    UserModel.username == settings.default_admin_username
                )
            )
            self.assertFalse(
                verify_password("icore1234!", admin.password_salt, admin.password_hash)
            )
            self.assertTrue(
                verify_password(
                    "new-strong-admin-password",
                    admin.password_salt,
                    admin.password_hash,
                )
            )
        engine.dispose()

    def test_schema_compatibility_adds_google_identity_columns_to_legacy_users(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username VARCHAR(100) NOT NULL,
                        password_salt VARCHAR(64) NOT NULL,
                        password_hash VARCHAR(128) NOT NULL,
                        role VARCHAR(30) NOT NULL,
                        is_active BOOLEAN NOT NULL,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )

        ensure_schema_compatibility(engine)
        ensure_schema_compatibility(engine)

        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("users")}
        self.assertTrue(
            {"email", "google_sub", "display_name", "last_login_at"}.issubset(columns)
        )
        unique_indexes = {
            tuple(index.get("column_names") or [])
            for index in inspector.get_indexes("users")
            if index.get("unique")
        }
        self.assertIn(("email",), unique_indexes)
        self.assertIn(("google_sub",), unique_indexes)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
