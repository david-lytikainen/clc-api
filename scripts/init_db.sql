CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    reset_token VARCHAR(255) UNIQUE,
    reset_token_expiration TIMESTAMP WITH TIME ZONE,
    forgot_password_code VARCHAR(6),
    forgot_password_code_expires_at TIMESTAMP WITH TIME ZONE,
    allergic_to_cinnamon BOOLEAN,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    CONSTRAINT fk_user_role FOREIGN KEY (role_id) REFERENCES roles(id)
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_role_id ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_reset_token ON users(reset_token);

insert into roles (name) values ('client'), ('admin');

-- Product types
CREATE TABLE IF NOT EXISTS product_types (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL
);

-- Product / catalog colors (reference)
CREATE TABLE IF NOT EXISTS colors (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    hex VARCHAR(7) NOT NULL
);

INSERT INTO colors (id, name, hex) VALUES
    (1, 'Classic Black', '#111014'),
    (2, 'Toasty Brown', '#413035'),
    (3, 'Honeycomb', '#B4793C'),
    (4, 'Olive', '#313024'),
    (5, 'Crimson', '#8E2825')
ON CONFLICT (id) DO NOTHING;

SELECT setval(
    pg_get_serial_sequence('colors', 'id'),
    COALESCE((SELECT MAX(id) FROM colors), 1)
);

-- Products
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    product_type_id INTEGER NOT NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    stripe_price_id VARCHAR(255),
    price NUMERIC(10,2) NOT NULL,
    dimensions VARCHAR(100),
    color VARCHAR(50),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_product_type FOREIGN KEY (product_type_id) REFERENCES product_types(id)
);

CREATE INDEX IF NOT EXISTS idx_products_product_type_id ON products(product_type_id);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    product_id INTEGER NOT NULL,
    color_id INTEGER NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    order_number VARCHAR(6),
    payment_intent_id VARCHAR(255),
    stripe_price_id VARCHAR(255),
    quantity INTEGER,
    amount_cents INTEGER,
    status VARCHAR(20),
    customer_email VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    paid_at TIMESTAMP WITH TIME ZONE,
    tracking_url VARCHAR(512),
    comments TEXT,
    allergic_to_cinnamon BOOLEAN,

    CONSTRAINT fk_order_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_order_product FOREIGN KEY (product_id) REFERENCES products(id),
    CONSTRAINT fk_order_color FOREIGN KEY (color_id) REFERENCES colors(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_session_id ON orders(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_order_number ON orders(order_number);

-- Product images
CREATE TABLE IF NOT EXISTS product_images (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL,
    s3_key VARCHAR(512) NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    color_id INTEGER NOT NULL,

    CONSTRAINT fk_product_image_product FOREIGN KEY (product_id) REFERENCES products(id),
    CONSTRAINT fk_product_image_color FOREIGN KEY (color_id) REFERENCES colors(id)
);

CREATE INDEX IF NOT EXISTS idx_product_images_product_id ON product_images(product_id);
CREATE INDEX IF NOT EXISTS idx_product_images_color_id ON product_images(color_id);

CREATE TABLE IF NOT EXISTS carts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    guest_token VARCHAR(255),
    items JSON NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_cart_user FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_carts_user_id ON carts(user_id);
CREATE INDEX IF NOT EXISTS idx_carts_guest_token ON carts(guest_token);

-- Home page banner pictures (S3); banner_index 0, 1, 2 = first, second, third slot
CREATE TABLE IF NOT EXISTS banner_pictures (
    id SERIAL PRIMARY KEY,
    s3_key VARCHAR(512) NOT NULL,
    banner_index INTEGER
);

CREATE INDEX IF NOT EXISTS idx_banner_pictures_banner_index ON banner_pictures(banner_index);

-- Home page footer pictures (S3); footer_index 0, 1 = first, second slot
CREATE TABLE IF NOT EXISTS footer_pictures (
    id SERIAL PRIMARY KEY,
    s3_key VARCHAR(512) NOT NULL,
    footer_index INTEGER
);

CREATE INDEX IF NOT EXISTS idx_footer_pictures_footer_index ON footer_pictures(footer_index);

