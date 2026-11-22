-- Tables the billing phase 2 migrations touch that billing_a0_schema.sql does not hold,
-- as they stand at alembic revision billing_p1_ledger_owner_check on SQLite. Generated with
-- the SQLite DDL of the models at commit b9806fc, before billing phase 2.
CREATE TABLE products (
	id INTEGER NOT NULL, 
	family_id INTEGER, 
	name VARCHAR(255) NOT NULL, 
	description TEXT, 
	code VARCHAR(128) NOT NULL, 
	overrides JSON NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	permission_set_id INTEGER, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_products_family_code UNIQUE (family_id, code), 
	FOREIGN KEY(family_id) REFERENCES product_families (id) ON DELETE SET NULL, 
	FOREIGN KEY(permission_set_id) REFERENCES permission_sets (id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX ix_products_code ON products (code);
CREATE INDEX ix_products_family_id ON products (family_id);
CREATE INDEX ix_products_id ON products (id);
CREATE INDEX ix_products_permission_set_id ON products (permission_set_id);
CREATE TABLE orders (
	id INTEGER NOT NULL, 
	order_number BIGINT NOT NULL, 
	billing_account_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	status VARCHAR(48) DEFAULT 'draft_cart' NOT NULL, 
	currency VARCHAR(3) DEFAULT 'USD' NOT NULL, 
	setup_cents BIGINT DEFAULT '0' NOT NULL, 
	recurring_cents BIGINT DEFAULT '0' NOT NULL, 
	tax_cents BIGINT DEFAULT '0' NOT NULL, 
	discount_cents BIGINT DEFAULT '0' NOT NULL, 
	total_cents BIGINT DEFAULT '0' NOT NULL, 
	coupon_code VARCHAR(64), 
	terms_version VARCHAR(64), 
	terms_accepted_at DATETIME, 
	checkout_nonce VARCHAR(128), 
	notes TEXT, 
	admin_notes TEXT, 
	accepted_at DATETIME, 
	accepted_by_user_id INTEGER, 
	fraud_score INTEGER, 
	client_ip VARCHAR(45), 
	invoice_id INTEGER, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (order_number), 
	FOREIGN KEY(billing_account_id) REFERENCES billing_accounts (id) ON DELETE RESTRICT, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT, 
	UNIQUE (checkout_nonce), 
	FOREIGN KEY(accepted_by_user_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(invoice_id) REFERENCES invoices (id) ON DELETE SET NULL
);
CREATE INDEX ix_orders_accepted_by_user_id ON orders (accepted_by_user_id);
CREATE INDEX ix_orders_billing_account_id ON orders (billing_account_id);
CREATE INDEX ix_orders_billing_account_status ON orders (billing_account_id, status);
CREATE INDEX ix_orders_coupon_code ON orders (coupon_code);
CREATE INDEX ix_orders_id ON orders (id);
CREATE INDEX ix_orders_invoice_id ON orders (invoice_id);
CREATE INDEX ix_orders_status ON orders (status);
CREATE INDEX ix_orders_user_id ON orders (user_id);
CREATE INDEX ix_orders_user_status ON orders (user_id, status);
CREATE TABLE billing_profiles (
	id INTEGER NOT NULL, 
	billing_account_id INTEGER NOT NULL, 
	legal_name VARCHAR(255), 
	company VARCHAR(255), 
	address_line1 VARCHAR(255), 
	address_line2 VARCHAR(255), 
	city VARCHAR(128), 
	region VARCHAR(128), 
	postal_code VARCHAR(32), 
	country VARCHAR(2), 
	phone VARCHAR(32), 
	tax_id VARCHAR(64), 
	invoice_email VARCHAR(320), 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_billing_profiles_account UNIQUE (billing_account_id), 
	FOREIGN KEY(billing_account_id) REFERENCES billing_accounts (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ix_billing_profiles_billing_account_id ON billing_profiles (billing_account_id);
CREATE INDEX ix_billing_profiles_id ON billing_profiles (id);
CREATE TABLE tax_rates (
	id INTEGER NOT NULL, 
	country VARCHAR(2) NOT NULL, 
	region VARCHAR(128), 
	rate_bps INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	enabled BOOLEAN DEFAULT '1' NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_tax_rates_country_region UNIQUE (country, region)
);
CREATE INDEX ix_tax_rates_country ON tax_rates (country);
CREATE INDEX ix_tax_rates_id ON tax_rates (id);
CREATE TABLE email_messages (
	id INTEGER NOT NULL, 
	billing_account_id INTEGER, 
	user_id INTEGER, 
	to_address VARCHAR(320) NOT NULL, 
	cc TEXT, 
	subject VARCHAR(998) NOT NULL, 
	body_text TEXT, 
	body_html TEXT, 
	template_key VARCHAR(128), 
	event VARCHAR(64), 
	status VARCHAR(32) DEFAULT 'queued' NOT NULL, 
	provider_message_id VARCHAR(255), 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	error TEXT, 
	related_type VARCHAR(64), 
	related_id VARCHAR(255), 
	idempotency_key VARCHAR(255), 
	sent_at DATETIME, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(billing_account_id) REFERENCES billing_accounts (id) ON DELETE SET NULL, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL, 
	UNIQUE (idempotency_key)
);
CREATE INDEX ix_email_messages_billing_account_id ON email_messages (billing_account_id);
CREATE INDEX ix_email_messages_event ON email_messages (event);
CREATE INDEX ix_email_messages_id ON email_messages (id);
CREATE INDEX ix_email_messages_provider_message_id ON email_messages (provider_message_id);
CREATE INDEX ix_email_messages_status ON email_messages (status);
CREATE INDEX ix_email_messages_status_created ON email_messages (status, created_at);
CREATE INDEX ix_email_messages_template_key ON email_messages (template_key);
CREATE INDEX ix_email_messages_to_address ON email_messages (to_address);
CREATE INDEX ix_email_messages_user_id ON email_messages (user_id);
CREATE TABLE plugin_installations (
	id VARCHAR(48) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	manifest JSON NOT NULL, 
	directory VARCHAR(255) NOT NULL, 
	code_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	granted_permissions JSON NOT NULL, 
	addon_refs JSON NOT NULL, 
	disabled_reason VARCHAR(500), 
	consecutive_failures INTEGER DEFAULT '0' NOT NULL, 
	total_failures INTEGER DEFAULT '0' NOT NULL, 
	circuit_opens INTEGER DEFAULT '0' NOT NULL, 
	circuit_open_until DATETIME, 
	last_error TEXT, 
	last_error_at DATETIME, 
	last_success_at DATETIME, 
	installed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id)
);
