import asyncio
import decimal
import sys
import uuid
from pathlib import Path

# Add apps/api to path so app modules can be imported
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "api"))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, hash_token
from app.db.base import utcnow
from app.db.session import get_sessionmaker
from app.models.billing import BillingAccount
from app.models.enums import Role, UserStatus
from app.models.org import Organization, OrganizationMember, Project
from app.models.provider import (
    HardwareInventory,
    Provider,
    ProviderCapability,
    ProviderMachine,
)
from app.models.user import User, UserRole
from app.services import ledger


async def seed_user(
    db: AsyncSession,
    *,
    email: str,
    password: str = "Password123!",
    roles: list[Role],
    org_name: str,
    project_name: str,
    credits_amount: decimal.Decimal,
) -> User:
    # Check if user exists
    res = await db.execute(select(User).where(User.email == email))
    user = res.scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(password),
            email_verified=True,
            status=UserStatus.active,
        )
        db.add(user)
        await db.flush()
        print(f"Created user {email} (id: {user.id})")
    else:
        user.password_hash = hash_password(password)
        user.email_verified = True
        user.status = UserStatus.active
        await db.flush()
        print(f"Updated user {email}")

    # Set roles
    for role in roles:
        role_res = await db.execute(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role == role)
        )
        if role_res.scalar_one_or_none() is None:
            db.add(UserRole(user_id=user.id, role=role))
            await db.flush()

    # Set organization & membership
    org_res = await db.execute(select(Organization).where(Organization.owner_id == user.id))
    org = org_res.scalar_one_or_none()
    if org is None:
        org = Organization(name=org_name, owner_id=user.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMember(org_id=org.id, user_id=user.id, role=Role.customer))
        await db.flush()

    # Set Project
    proj_res = await db.execute(select(Project).where(Project.owner_id == user.id))
    proj = proj_res.scalar_one_or_none()
    if proj is None:
        proj = Project(
            name=project_name,
            owner_id=user.id,
            org_id=org.id,
        )
        db.add(proj)
        await db.flush()

    # Fund Customer Credits Account
    if credits_amount > 0:
        cust_acct = await ledger.get_or_create_customer_account(db, user.id)
        external = await ledger.get_platform_account(db, "external")
        grant_minor = ledger.to_minor(credits_amount)
        if cust_acct.balance_minor < grant_minor:
            top_up = grant_minor - cust_acct.balance_minor
            await ledger.post_transaction(
                db,
                kind="credit_purchase",
                legs=[
                    (external, "debit", top_up),
                    (cust_acct, "credit", top_up),
                ],
                idempotency_key=f"seed_funding:{user.id}:{top_up}",
                reference_type="seed_funding",
                reference_id=str(user.id),
            )
            await db.refresh(cust_acct)
            print(f"Funded {email} with ${ledger.minor_to_str(cust_acct.balance_minor)} credits")

    return user


async def seed_provider_machines(db: AsyncSession, provider_user: User):
    prov_res = await db.execute(select(Provider).where(Provider.user_id == provider_user.id))
    provider = prov_res.scalar_one_or_none()
    if provider is None:
        provider = Provider(
            user_id=provider_user.id,
            display_name="Apex Global Compute Cluster",
            contact_email=provider_user.email,
            status="active",
        )
        db.add(provider)
        await db.flush()

    # Machine 1: H100 SXM5
    m1_res = await db.execute(
        select(ProviderMachine).where(
            ProviderMachine.provider_id == provider.id,
            ProviderMachine.hostname == f"node-h100-sxm5-{provider_user.email.split('@')[0]}",
        )
    )
    m1 = m1_res.scalar_one_or_none()
    if m1 is None:
        m1 = ProviderMachine(
            provider_id=provider.id,
            hostname=f"node-h100-sxm5-{provider_user.email.split('@')[0]}",
            agent_version="0.1.0",
            status="registered",
            availability="online",
            asking_price_per_hour=decimal.Decimal("2.40"),
            region="us-east-1",
            signing_key=hash_token("dev-signing-key"),
            secret_hash=hash_token("dev-machine-secret"),
            last_heartbeat_at=utcnow(),
        )
        db.add(m1)
        await db.flush()
        db.add(
            HardwareInventory(
                machine_id=m1.id,
                gpu_count=8,
                gpu_model="NVIDIA H100 80GB SXM5",
                vram_mb=81920,
                cpu_cores=128,
                ram_mb=1048576,
                disk_gb=16384,
                verification_status="verified",
            )
        )
        db.add(
            ProviderCapability(
                machine_id=m1.id,
                supported_workloads=["embeddings", "text-generation"],
                verification_status="verified",
            )
        )
        await db.flush()

    # Machine 2: A100 SXM4
    m2_res = await db.execute(
        select(ProviderMachine).where(
            ProviderMachine.provider_id == provider.id,
            ProviderMachine.hostname == f"node-a100-80gb-{provider_user.email.split('@')[0]}",
        )
    )
    m2 = m2_res.scalar_one_or_none()
    if m2 is None:
        m2 = ProviderMachine(
            provider_id=provider.id,
            hostname=f"node-a100-80gb-{provider_user.email.split('@')[0]}",
            agent_version="0.1.0",
            status="registered",
            availability="online",
            asking_price_per_hour=decimal.Decimal("1.20"),
            region="us-west-2",
            signing_key=hash_token("dev-signing-key-2"),
            secret_hash=hash_token("dev-machine-secret-2"),
            last_heartbeat_at=utcnow(),
        )
        db.add(m2)
        await db.flush()
        db.add(
            HardwareInventory(
                machine_id=m2.id,
                gpu_count=4,
                gpu_model="NVIDIA A100-SXM4-80GB",
                vram_mb=81920,
                cpu_cores=64,
                ram_mb=524288,
                disk_gb=8192,
                verification_status="verified",
            )
        )
        db.add(
            ProviderCapability(
                machine_id=m2.id,
                supported_workloads=["text-generation"],
                verification_status="verified",
            )
        )
        await db.flush()

    # Provider earnings balance
    prov_acct = await ledger.get_provider_account(db, provider.id)
    external = await ledger.get_platform_account(db, "external")
    target_earnings = ledger.to_minor(decimal.Decimal("1840.50"))
    if prov_acct.balance_minor < target_earnings:
        delta = target_earnings - prov_acct.balance_minor
        await ledger.post_transaction(
            db,
            kind="credit_purchase",
            legs=[
                (external, "debit", delta),
                (prov_acct, "credit", delta),
            ],
            idempotency_key=f"seed_provider_earnings:{provider.id}",
            reference_type="seed_provider_earnings",
            reference_id=str(provider.id),
        )
        await db.refresh(prov_acct)
        print(f"Credited provider with ${ledger.minor_to_str(prov_acct.balance_minor)} earnings")


async def main():
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        # 1. Admin accounts
        await seed_user(
            db,
            email="admin@computecoin.internal",
            password="Password123!",
            roles=[Role.administrator, Role.customer, Role.provider],
            org_name="ComputeCoin Operations",
            project_name="Platform Core & Telemetry",
            credits_amount=decimal.Decimal("10000.00"),
        )
        await seed_user(
            db,
            email="admin@dev.io",
            password="Password123!",
            roles=[Role.administrator, Role.customer, Role.provider],
            org_name="ComputeCoin Operations",
            project_name="Platform Core & Telemetry",
            credits_amount=decimal.Decimal("10000.00"),
        )

        # 2. Customer accounts
        await seed_user(
            db,
            email="customer@computecoin.internal",
            password="Password123!",
            roles=[Role.customer],
            org_name="Acme AI Technologies",
            project_name="Production LLM Pipeline",
            credits_amount=decimal.Decimal("2500.00"),
        )
        await seed_user(
            db,
            email="customer@dev.io",
            password="Password123!",
            roles=[Role.customer],
            org_name="Acme AI Technologies",
            project_name="Production LLM Pipeline",
            credits_amount=decimal.Decimal("2500.00"),
        )

        # 3. Provider accounts
        prov1 = await seed_user(
            db,
            email="provider@computecoin.internal",
            password="Password123!",
            roles=[Role.provider],
            org_name="Apex GPU Network",
            project_name="Apex Fleet",
            credits_amount=decimal.Decimal("100.00"),
        )
        await seed_provider_machines(db, prov1)

        prov2 = await seed_user(
            db,
            email="provider@dev.io",
            password="Password123!",
            roles=[Role.provider],
            org_name="Apex GPU Network",
            project_name="Apex Fleet",
            credits_amount=decimal.Decimal("100.00"),
        )
        await seed_provider_machines(db, prov2)

        await db.commit()
        print("\nAll dev accounts and hardware inventory seeded successfully!")


if __name__ == "__main__":
    asyncio.run(main())
