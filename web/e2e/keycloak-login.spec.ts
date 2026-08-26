import { expect, test, type Page } from "@playwright/test";

const credentials = {
  firstUsername: process.env.E2E_KEYCLOAK_USERNAME ?? "",
  firstPassword: process.env.E2E_KEYCLOAK_PASSWORD ?? "",
  secondUsername: process.env.E2E_SECOND_KEYCLOAK_USERNAME ?? "",
  secondPassword: process.env.E2E_SECOND_KEYCLOAK_PASSWORD ?? "",
  secondSubject: process.env.E2E_SECOND_SUBJECT_ID ?? "",
  firstTenant: process.env.E2E_EXPECTED_TENANT ?? "",
  secondTenant: process.env.E2E_SECOND_EXPECTED_TENANT ?? "",
  document: process.env.E2E_EXPECTED_DOCUMENT ?? "",
  question: process.env.E2E_STREAM_QUESTION ?? "",
};

const configured = Object.values(credentials).every(Boolean);

async function login(page: Page, username: string, password: string): Promise<void> {
  await page.getByRole("button", { name: "使用 Keycloak 登录" }).click();
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await expect(page.getByText("当前账号", { exact: true })).toBeVisible();
}

async function logout(page: Page): Promise<void> {
  await page.getByRole("button", { name: "退出当前账号" }).click();
  await expect(page.getByRole("button", { name: "使用 Keycloak 登录" })).toBeVisible();
}

async function selectTenant(page: Page, tenantName: string): Promise<string> {
  const selector = page.getByLabel("当前租户");
  const option = selector.locator("option").filter({ hasText: tenantName });
  await expect(option).toHaveCount(1);
  const tenantId = await option.getAttribute("value");
  expect(tenantId).toBeTruthy();
  await selector.selectOption(tenantId!);
  await expect(selector).toHaveValue(tenantId!);
  return tenantId!;
}

async function openMembersConsole(page: Page, tenantName: string): Promise<void> {
  await selectTenant(page, tenantName);
  await page.getByRole("button", { name: "租户管理" }).click();
  await page.getByRole("button", { name: "成员与权限" }).click();
  await expect(page.getByRole("heading", { name: "成员与角色" })).toBeVisible();
}

async function ensureSecondSubjectMembership(page: Page): Promise<void> {
  await openMembersConsole(page, credentials.secondTenant);
  const row = page.getByRole("row").filter({ hasText: credentials.secondSubject });
  if (await row.count() === 0) {
    await page.getByLabel("登录标识").fill(credentials.secondSubject);
    await page.getByLabel("显示名称").fill(credentials.secondUsername);
    await page.getByLabel("租户角色").selectOption("reader");
    await page.getByRole("button", { name: "分配成员" }).click();
    await expect(row).toBeVisible();
  } else if (await row.getByRole("button", { name: "重新启用" }).count()) {
    await row.getByRole("button", { name: "重新启用" }).click();
    await expect(row.getByRole("button", { name: "移除" })).toBeVisible();
  }
}

async function removeSecondSubjectMembership(page: Page): Promise<void> {
  await openMembersConsole(page, credentials.secondTenant);
  const row = page.getByRole("row").filter({ hasText: credentials.secondSubject });
  await expect(row).toBeVisible();
  if (await row.getByRole("button", { name: "移除" }).count()) {
    page.once("dialog", (dialog) => dialog.accept());
    await row.getByRole("button", { name: "移除" }).click();
    await expect(row.getByRole("button", { name: "重新启用" })).toBeVisible();
  }
}

test.describe("真实 Keycloak 浏览器验收", () => {
  test.skip(!configured, "需要通过 E2E_* 环境变量秘密注入两个测试主体和验收数据名称");

  test("登录、恢复、认证请求、流式问答、下载、续期、退出和成员撤销", async ({ page }) => {
    let protectedBearerObserved = false;
    let streamBearerObserved = false;
    let bearer = "";
    page.on("request", (request) => {
      const authorization = request.headers().authorization ?? "";
      const authorizationPresent = /^Bearer\s+\S+$/.test(authorization);
      if (authorizationPresent) bearer = authorization;
      if (request.url().includes("/api/v1/tenants")) protectedBearerObserved ||= authorizationPresent;
      if (request.url().includes("/api/v1/chat/query")) streamBearerObserved ||= authorizationPresent;
    });

    await page.goto("/");
    await expect(page.getByRole("button", { name: "使用 Keycloak 登录" })).toBeVisible();
    await login(page, credentials.firstUsername, credentials.firstPassword);
    await ensureSecondSubjectMembership(page);
    const firstTenantId = await selectTenant(page, credentials.firstTenant);
    await expect.poll(() => protectedBearerObserved).toBe(true);

    const persistedKeys = await page.evaluate(() => Object.keys(localStorage));
    expect(persistedKeys.some((key) => /(access|refresh|id)[-_]?token|authorization[-_]?code/i.test(key))).toBe(false);

    await page.reload();
    await expect(page.getByText("当前账号", { exact: true })).toBeVisible();
    await expect(page.getByLabel("当前租户")).toContainText(credentials.firstTenant);

    await page.getByRole("button", { name: /知识/ }).click();
    const documentRow = page.getByRole("row").filter({ hasText: credentials.document });
    await expect(documentRow).toBeVisible();
    await documentRow.getByRole("button", { name: "版本与预览" }).click();
    await expect(page.getByRole("button", { name: "关闭文档预览" })).toBeVisible();
    await page.getByRole("button", { name: "关闭文档预览" }).click();
    const download = page.waitForEvent("download");
    await documentRow.getByRole("button", { name: "下载" }).click();
    await download;

    await page.getByRole("button", { name: /问答/ }).click();
    await expect(page.getByRole("button", { name: "发送" })).toBeEnabled();
    await page.getByPlaceholder("询问企业政策、IT 帮助或产品资料…").fill(credentials.question);
    await page.getByRole("button", { name: "发送" }).click();
    await expect.poll(() => streamBearerObserved).toBe(true);
    await expect(page.locator(".message.assistant").last()).not.toHaveText("");

    const expiryWait = Number(process.env.E2E_TOKEN_EXPIRY_WAIT_MS ?? "65000");
    await page.waitForTimeout(expiryWait);
    await page.reload();
    await expect(page.getByText("当前账号", { exact: true })).toBeVisible();
    await expect(page.getByLabel("当前租户")).toContainText(credentials.firstTenant);

    await logout(page);
    await login(page, credentials.secondUsername, credentials.secondPassword);
    const secondTenantId = await selectTenant(page, credentials.secondTenant);
    expect(secondTenantId).not.toBe(firstTenantId);
    await expect(page.getByLabel("当前租户").locator("option").filter({ hasText: credentials.firstTenant })).toHaveCount(0);

    await expect.poll(() => bearer).toMatch(/^Bearer\s+\S+$/);
    const forbidden = await page.request.get("/api/v1/tenant/context", {
      headers: { Authorization: bearer, "X-Tenant-ID": firstTenantId },
    });
    expect(forbidden.status()).toBe(403);

    await logout(page);
    await login(page, credentials.firstUsername, credentials.firstPassword);
    await removeSecondSubjectMembership(page);
    await logout(page);

    bearer = "";
    await login(page, credentials.secondUsername, credentials.secondPassword);
    await expect(page.getByLabel("当前租户")).toContainText("无可用租户");
    await expect.poll(() => bearer).toMatch(/^Bearer\s+\S+$/);
    const revoked = await page.request.get("/api/v1/tenant/context", {
      headers: { Authorization: bearer, "X-Tenant-ID": secondTenantId },
    });
    expect(revoked.status()).toBe(403);
  });
});
