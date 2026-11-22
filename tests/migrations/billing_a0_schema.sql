-- Tables touched by the billing phase 1 migrations (and the tables they reference),
-- as they stand at alembic revision billing_a0_renewals on SQLite. Generated with
-- Base.metadata.create_all from the models at commit ca24629, before billing phase 1.
CREATE TABLE users (
	id INTEGER NOT NULL, 
	username VARCHAR(255) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	password VARCHAR(255), 
	is_admin BOOLEAN NOT NULL, 
	is_reseller BOOLEAN DEFAULT '0' NOT NULL, 
	require_2fa BOOLEAN DEFAULT '0' NOT NULL, 
	reseller_id INTEGER, 
	permission_set_id INTEGER, 
	billing_integration_id INTEGER, 
	external_user_id VARCHAR(255), 
	external_username VARCHAR(255), 
	external_email VARCHAR(255), 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_users_billing_identity UNIQUE (billing_integration_id, external_user_id), 
	CONSTRAINT uq_users_reseller_identity UNIQUE (reseller_id, external_user_id), 
	CONSTRAINT fk_users_reseller_id FOREIGN KEY(reseller_id) REFERENCES resellers (id) ON DELETE SET NULL, 
	FOREIGN KEY(permission_set_id) REFERENCES permission_sets (id) ON DELETE SET NULL, 
	FOREIGN KEY(billing_integration_id) REFERENCES billing_integrations (id) ON DELETE SET NULL
);
CREATE INDEX ix_users_reseller_id ON users (reseller_id);
CREATE UNIQUE INDEX ix_users_username ON users (username);
CREATE INDEX ix_users_id ON users (id);
CREATE INDEX ix_users_permission_set_id ON users (permission_set_id);
CREATE UNIQUE INDEX ix_users_email ON users (email);
CREATE INDEX ix_users_billing_integration_id ON users (billing_integration_id);
CREATE INDEX ix_users_external_user_id ON users (external_user_id);
CREATE TABLE resellers (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	group_id INTEGER, 
	api_key_hash VARCHAR(64), 
	api_key_prefix VARCHAR(16), 
	stripe_customer_ref VARCHAR(255), 
	cached_balance_cents BIGINT DEFAULT '0' NOT NULL, 
	charge_preference VARCHAR(32) DEFAULT 'credit_first' NOT NULL, 
	status VARCHAR(32) DEFAULT 'active' NOT NULL, 
	nonpayment_policy VARCHAR(32) DEFAULT 'block_new' NOT NULL, 
	billing_hold BOOLEAN DEFAULT '0' NOT NULL, 
	billing_hold_at DATETIME, 
	billing_hold_cleared_at DATETIME, 
	billing_hold_reason VARCHAR(512), 
	last_used_at DATETIME, 
	last_used_ip VARCHAR(45), 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	FOREIGN KEY(group_id) REFERENCES reseller_groups (id) ON DELETE SET NULL, 
	UNIQUE (api_key_hash)
);
CREATE INDEX ix_resellers_api_key_prefix ON resellers (api_key_prefix);
CREATE INDEX ix_resellers_group_id ON resellers (group_id);
CREATE UNIQUE INDEX ix_resellers_stripe_customer_ref ON resellers (stripe_customer_ref);
CREATE INDEX ix_resellers_status_group ON resellers (status, group_id);
CREATE INDEX ix_resellers_id ON resellers (id);
CREATE INDEX ix_resellers_status ON resellers (status);
CREATE TABLE billing_accounts (
	id INTEGER NOT NULL, 
	account_type VARCHAR(32) NOT NULL, 
	user_id INTEGER, 
	reseller_id INTEGER, 
	status VARCHAR(32) DEFAULT 'active' NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	credit_balance_cents BIGINT DEFAULT '0' NOT NULL, 
	tax_exempt BOOLEAN DEFAULT '0' NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_billing_accounts_type_owner CHECK ((account_type = 'client' AND user_id IS NOT NULL) OR (account_type = 'reseller' AND reseller_id IS NOT NULL)), 
	CONSTRAINT uq_billing_accounts_reseller UNIQUE (reseller_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	UNIQUE (reseller_id), 
	FOREIGN KEY(reseller_id) REFERENCES resellers (id) ON DELETE RESTRICT
);
CREATE INDEX ix_billing_accounts_status ON billing_accounts (status);
CREATE INDEX ix_billing_accounts_user_id ON billing_accounts (user_id);
CREATE INDEX ix_billing_accounts_id ON billing_accounts (id);
CREATE INDEX ix_billing_accounts_account_type ON billing_accounts (account_type);
CREATE TABLE services (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	external_service_id VARCHAR(255), 
	owner_user_id INTEGER, 
	service_type VARCHAR(11) NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	description TEXT, 
	config JSON, 
	product_code VARCHAR(128), 
	os_code VARCHAR(128), 
	product_snapshot JSON, 
	permission_set_id INTEGER, 
	permission_overrides JSON, 
	provisioning_source VARCHAR(8) DEFAULT 'billing' NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	terminated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(permission_set_id) REFERENCES permission_sets (id) ON DELETE SET NULL
);
CREATE INDEX ix_services_owner_user_id ON services (owner_user_id);
CREATE INDEX ix_services_external_service_id ON services (external_service_id);
CREATE INDEX ix_services_os_code ON services (os_code);
CREATE INDEX ix_services_permission_set_id ON services (permission_set_id);
CREATE INDEX ix_services_provisioning_source ON services (provisioning_source);
CREATE INDEX ix_services_id ON services (id);
CREATE INDEX ix_services_name ON services (name);
CREATE INDEX ix_services_product_code ON services (product_code);
CREATE INDEX ix_services_service_type ON services (service_type);
CREATE TABLE service_billings (
	id INTEGER NOT NULL, 
	service_id INTEGER NOT NULL, 
	reseller_id INTEGER, 
	billing_account_id INTEGER, 
	product_id INTEGER, 
	setup_price_cents BIGINT DEFAULT '0' NOT NULL, 
	monthly_price_cents BIGINT NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	next_charge_at DATETIME, 
	billing_anchor_day INTEGER, 
	cycle_interval VARCHAR(32), 
	interval_months INTEGER, 
	cancel_at DATETIME, 
	status VARCHAR(32) DEFAULT 'active' NOT NULL, 
	failed_attempts INTEGER DEFAULT '0' NOT NULL, 
	last_failure_code VARCHAR(128), 
	last_failure_message TEXT, 
	grace_until DATETIME, 
	next_retry_at DATETIME, 
	last_charged_at DATETIME, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_service_billings_setup_nonnegative CHECK (setup_price_cents >= 0), 
	CONSTRAINT ck_service_billings_monthly_nonnegative CHECK (monthly_price_cents >= 0), 
	CONSTRAINT ck_service_billings_failures_nonnegative CHECK (failed_attempts >= 0), 
	CONSTRAINT ck_service_billings_anchor_day CHECK (billing_anchor_day IS NULL OR (billing_anchor_day >= 1 AND billing_anchor_day <= 31)), 
	UNIQUE (service_id), 
	FOREIGN KEY(service_id) REFERENCES services (id) ON DELETE RESTRICT, 
	FOREIGN KEY(reseller_id) REFERENCES resellers (id) ON DELETE RESTRICT, 
	FOREIGN KEY(billing_account_id) REFERENCES billing_accounts (id) ON DELETE SET NULL, 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE SET NULL
);
CREATE INDEX ix_service_billings_billing_account_status ON service_billings (billing_account_id, status);
CREATE INDEX ix_service_billings_billing_account_id ON service_billings (billing_account_id);
CREATE INDEX ix_service_billings_next_charge_at ON service_billings (next_charge_at);
CREATE INDEX ix_service_billings_next_retry_at ON service_billings (next_retry_at);
CREATE INDEX ix_service_billings_product_id ON service_billings (product_id);
CREATE INDEX ix_service_billings_id ON service_billings (id);
CREATE INDEX ix_service_billings_status_next_charge ON service_billings (status, next_charge_at);
CREATE INDEX ix_service_billings_cancel_at ON service_billings (cancel_at);
CREATE INDEX ix_service_billings_status ON service_billings (status);
CREATE INDEX ix_service_billings_reseller_status ON service_billings (reseller_id, status);
CREATE INDEX ix_service_billings_reseller_id ON service_billings (reseller_id);
CREATE TABLE price_plans (
	id INTEGER NOT NULL, 
	frontend_product_id INTEGER NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	pricing_model VARCHAR(32) NOT NULL, 
	setup_cents INTEGER DEFAULT '0' NOT NULL, 
	trial_days INTEGER, 
	enabled BOOLEAN DEFAULT '1' NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(frontend_product_id) REFERENCES frontend_products (id) ON DELETE CASCADE
);
CREATE INDEX ix_price_plans_frontend_product_id ON price_plans (frontend_product_id);
CREATE INDEX ix_price_plans_id ON price_plans (id);
CREATE TABLE price_plan_cycles (
	id INTEGER NOT NULL, 
	price_plan_id INTEGER NOT NULL, 
	interval VARCHAR(32) NOT NULL, 
	price_cents INTEGER NOT NULL, 
	enabled BOOLEAN DEFAULT '1' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_price_plan_cycles_plan_interval UNIQUE (price_plan_id, interval), 
	FOREIGN KEY(price_plan_id) REFERENCES price_plans (id) ON DELETE CASCADE
);
CREATE INDEX ix_price_plan_cycles_price_plan_id ON price_plan_cycles (price_plan_id);
CREATE INDEX ix_price_plan_cycles_id ON price_plan_cycles (id);
CREATE TABLE order_items (
	id INTEGER NOT NULL, 
	order_id INTEGER NOT NULL, 
	frontend_product_id INTEGER NOT NULL, 
	price_plan_id INTEGER NOT NULL, 
	cycle_interval VARCHAR(32), 
	quantity INTEGER DEFAULT '1' NOT NULL, 
	setup_cents BIGINT DEFAULT '0' NOT NULL, 
	recurring_cents BIGINT DEFAULT '0' NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	config JSON NOT NULL, 
	name_snapshot VARCHAR(255) NOT NULL, 
	fulfill_status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	service_id INTEGER, 
	error_message TEXT, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES orders (id) ON DELETE CASCADE, 
	FOREIGN KEY(frontend_product_id) REFERENCES frontend_products (id) ON DELETE RESTRICT, 
	FOREIGN KEY(price_plan_id) REFERENCES price_plans (id) ON DELETE RESTRICT, 
	FOREIGN KEY(service_id) REFERENCES services (id) ON DELETE SET NULL
);
CREATE INDEX ix_order_items_price_plan_id ON order_items (price_plan_id);
CREATE INDEX ix_order_items_service_id ON order_items (service_id);
CREATE INDEX ix_order_items_order_id ON order_items (order_id);
CREATE INDEX ix_order_items_fulfill_status ON order_items (fulfill_status);
CREATE INDEX ix_order_items_frontend_product_id ON order_items (frontend_product_id);
CREATE INDEX ix_order_items_id ON order_items (id);
CREATE TABLE invoices (
	id INTEGER NOT NULL, 
	invoice_number BIGINT NOT NULL, 
	reseller_id INTEGER, 
	service_id INTEGER, 
	billing_account_id INTEGER, 
	order_id INTEGER, 
	purpose VARCHAR(32) NOT NULL, 
	status VARCHAR(32) DEFAULT 'draft' NOT NULL, 
	amount_cents BIGINT NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	description TEXT, 
	due_at DATETIME, 
	paid_at DATETIME, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_invoices_positive_amount CHECK (amount_cents > 0), 
	UNIQUE (invoice_number), 
	FOREIGN KEY(reseller_id) REFERENCES resellers (id) ON DELETE RESTRICT, 
	FOREIGN KEY(service_id) REFERENCES services (id) ON DELETE SET NULL, 
	FOREIGN KEY(billing_account_id) REFERENCES billing_accounts (id) ON DELETE SET NULL
);
CREATE INDEX ix_invoices_reseller_status_due ON invoices (reseller_id, status, due_at);
CREATE INDEX ix_invoices_id ON invoices (id);
CREATE INDEX ix_invoices_service_id ON invoices (service_id);
CREATE INDEX ix_invoices_status ON invoices (status);
CREATE INDEX ix_invoices_purpose ON invoices (purpose);
CREATE INDEX ix_invoices_order_id ON invoices (order_id);
CREATE INDEX ix_invoices_billing_account_id ON invoices (billing_account_id);
CREATE INDEX ix_invoices_reseller_id ON invoices (reseller_id);
CREATE TABLE invoice_sequences (
	name VARCHAR(64) NOT NULL, 
	next_value BIGINT NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (name)
);
CREATE TABLE invoice_lines (
	id INTEGER NOT NULL, 
	invoice_id INTEGER NOT NULL, 
	description VARCHAR(512) NOT NULL, 
	quantity INTEGER DEFAULT '1' NOT NULL, 
	unit_cents BIGINT NOT NULL, 
	total_cents BIGINT NOT NULL, 
	tax_cents BIGINT DEFAULT '0' NOT NULL, 
	sort_order INTEGER DEFAULT '0' NOT NULL, 
	order_item_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE CASCADE, 
	FOREIGN KEY(order_item_id) REFERENCES order_items (id) ON DELETE SET NULL
);
CREATE INDEX ix_invoice_lines_invoice_id ON invoice_lines (invoice_id);
CREATE INDEX ix_invoice_lines_id ON invoice_lines (id);
CREATE INDEX ix_invoice_lines_order_item_id ON invoice_lines (order_item_id);
CREATE TABLE payments (
	id INTEGER NOT NULL, 
	invoice_id INTEGER NOT NULL, 
	gateway VARCHAR(64) NOT NULL, 
	status VARCHAR(32) DEFAULT 'pending' NOT NULL, 
	amount_cents BIGINT NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	external_ref VARCHAR(255), 
	failure_code VARCHAR(128), 
	failure_message TEXT, 
	metadata JSON NOT NULL, 
	processed_at DATETIME, 
	refunded_at DATETIME, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_payments_positive_amount CHECK (amount_cents > 0), 
	FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE RESTRICT, 
	UNIQUE (external_ref)
);
CREATE INDEX ix_payments_gateway ON payments (gateway);
CREATE INDEX ix_payments_id ON payments (id);
CREATE INDEX ix_payments_invoice_id ON payments (invoice_id);
CREATE INDEX ix_payments_status ON payments (status);
CREATE INDEX ix_payments_invoice_status ON payments (invoice_id, status);
CREATE TABLE credit_ledger_entries (
	id INTEGER NOT NULL, 
	reseller_id INTEGER NOT NULL, 
	entry_type VARCHAR(32) NOT NULL, 
	amount_cents BIGINT NOT NULL, 
	balance_after_cents BIGINT NOT NULL, 
	description VARCHAR(512), 
	invoice_id INTEGER, 
	payment_id INTEGER, 
	service_id INTEGER, 
	reversal_of_id INTEGER, 
	reference_type VARCHAR(64), 
	reference_id VARCHAR(255), 
	idempotency_key VARCHAR(255), 
	metadata JSON NOT NULL, 
	created_by_user_id INTEGER, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_credit_ledger_nonzero_amount CHECK (amount_cents <> 0), 
	FOREIGN KEY(reseller_id) REFERENCES resellers (id) ON DELETE RESTRICT, 
	FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE SET NULL, 
	FOREIGN KEY(payment_id) REFERENCES payments (id) ON DELETE SET NULL, 
	FOREIGN KEY(service_id) REFERENCES services (id) ON DELETE SET NULL, 
	UNIQUE (reversal_of_id), 
	FOREIGN KEY(reversal_of_id) REFERENCES credit_ledger_entries (id) ON DELETE RESTRICT, 
	UNIQUE (idempotency_key), 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_credit_ledger_entries_entry_type ON credit_ledger_entries (entry_type);
CREATE INDEX ix_credit_ledger_entries_created_at ON credit_ledger_entries (created_at);
CREATE INDEX ix_credit_ledger_entries_id ON credit_ledger_entries (id);
CREATE INDEX ix_credit_ledger_reseller_created ON credit_ledger_entries (reseller_id, created_at);
CREATE INDEX ix_credit_ledger_entries_payment_id ON credit_ledger_entries (payment_id);
CREATE INDEX ix_credit_ledger_entries_created_by_user_id ON credit_ledger_entries (created_by_user_id);
CREATE INDEX ix_credit_ledger_entries_service_id ON credit_ledger_entries (service_id);
CREATE INDEX ix_credit_ledger_entries_invoice_id ON credit_ledger_entries (invoice_id);
CREATE INDEX ix_credit_ledger_entries_reseller_id ON credit_ledger_entries (reseller_id);
