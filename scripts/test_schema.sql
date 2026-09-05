--
-- PostgreSQL database dump
--

\restrict eTUL6e4u3IQ8waGc0rWNRl670EvCK1kWYaYTyEbzAeMsO4fEAiHFw79f9D7wjo1

-- Dumped from database version 18.6
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: activity_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.activity_logs (
    id integer NOT NULL,
    user_id integer,
    action character varying(100) NOT NULL,
    entity_type character varying(100),
    entity_id integer,
    description text,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


ALTER TABLE public.activity_logs OWNER TO postgres;

--
-- Name: activity_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.activity_logs ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.activity_logs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: authors; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.authors (
    id integer NOT NULL,
    name character varying(255) NOT NULL
);


ALTER TABLE public.authors OWNER TO postgres;

--
-- Name: authors_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.authors ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.authors_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: book_contents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.book_contents (
    id integer NOT NULL,
    volume_id integer NOT NULL,
    parent_id integer,
    title character varying(500) NOT NULL,
    content_type character varying(50) DEFAULT 'chapter'::character varying NOT NULL,
    page_number integer,
    sort_order integer DEFAULT 0 NOT NULL
);


ALTER TABLE public.book_contents OWNER TO postgres;

--
-- Name: book_contents_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.book_contents ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.book_contents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: book_copies; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.book_copies (
    id integer NOT NULL,
    volume_id integer NOT NULL,
    shelf_id integer,
    copy_code character varying(50) NOT NULL,
    status character varying(30) DEFAULT 'Available'::character varying NOT NULL,
    acquisition_date date,
    notes text,
    CONSTRAINT check_copy_status CHECK (((status)::text = ANY ((ARRAY['Available'::character varying, 'Issued'::character varying, 'Lost'::character varying, 'Damaged'::character varying, 'Missing'::character varying, 'Transferred'::character varying])::text[])))
);


ALTER TABLE public.book_copies OWNER TO postgres;

--
-- Name: book_copies_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.book_copies ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.book_copies_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: book_volumes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.book_volumes (
    id integer NOT NULL,
    book_id integer NOT NULL,
    volume_number integer NOT NULL,
    title character varying(255) NOT NULL
);


ALTER TABLE public.book_volumes OWNER TO postgres;

--
-- Name: book_volumes_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.book_volumes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.book_volumes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: books; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.books (
    id integer NOT NULL,
    title character varying(500) NOT NULL,
    author_id integer NOT NULL,
    category_id integer,
    publisher_id integer,
    cover_image character varying(255),
    archived_at timestamp with time zone
);


ALTER TABLE public.books OWNER TO postgres;

--
-- Name: books_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.books ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.books_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: borrowers; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.borrowers (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    phone character varying(30) NOT NULL,
    borrower_type character varying(30) NOT NULL,
    registration_no character varying(100),
    department character varying(255),
    address text,
    notes text,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT check_borrower_type CHECK (((borrower_type)::text = ANY ((ARRAY['Student'::character varying, 'Teacher'::character varying, 'Staff'::character varying, 'Other'::character varying])::text[])))
);


ALTER TABLE public.borrowers OWNER TO postgres;

--
-- Name: borrowers_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.borrowers ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.borrowers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: categories; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.categories (
    id integer NOT NULL,
    name character varying(100) NOT NULL
);


ALTER TABLE public.categories OWNER TO postgres;

--
-- Name: categories_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.categories ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.categories_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: loans; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.loans (
    id integer NOT NULL,
    copy_id integer NOT NULL,
    borrower_id integer NOT NULL,
    issue_date date DEFAULT CURRENT_DATE NOT NULL,
    due_date date NOT NULL,
    return_date date,
    issued_by integer,
    returned_to integer,
    notes text,
    CONSTRAINT check_due_date_not_before_issue CHECK ((due_date >= issue_date)),
    CONSTRAINT check_return_date_not_before_issue CHECK (((return_date IS NULL) OR (return_date >= issue_date)))
);


ALTER TABLE public.loans OWNER TO postgres;

--
-- Name: loans_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.loans ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.loans_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: locations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.locations (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    description text
);


ALTER TABLE public.locations OWNER TO postgres;

--
-- Name: locations_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.locations ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.locations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: publishers; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.publishers (
    id integer NOT NULL,
    name character varying(255) NOT NULL,
    city character varying(100)
);


ALTER TABLE public.publishers OWNER TO postgres;

--
-- Name: publishers_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.publishers ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.publishers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: shelves; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.shelves (
    id integer NOT NULL,
    location_id integer NOT NULL,
    shelf_code character varying(50) NOT NULL,
    description text
);


ALTER TABLE public.shelves OWNER TO postgres;

--
-- Name: shelves_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.shelves ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.shelves_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying(100) NOT NULL,
    full_name character varying(255) NOT NULL,
    password_hash text NOT NULL,
    role character varying(30) DEFAULT 'Assistant'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_login timestamp without time zone,
    theme_preference character varying(10),
    CONSTRAINT check_user_role CHECK (((role)::text = ANY ((ARRAY['Admin'::character varying, 'Librarian'::character varying, 'Assistant'::character varying])::text[])))
);


ALTER TABLE public.users OWNER TO postgres;


--
-- Name: organization_settings; Type: TABLE; Schema: public; Owner: postgres
--
-- Single-row branding table (see library.models.OrganizationSettings).
-- The CHECK on id enforces the singleton at the database level.
--

CREATE TABLE public.organization_settings (
    id integer NOT NULL,
    name character varying(255) DEFAULT ''::character varying NOT NULL,
    logo character varying(255),
    favicon character varying(255),
    primary_color character varying(7) DEFAULT ''::character varying NOT NULL,
    secondary_color character varying(7) DEFAULT ''::character varying NOT NULL,
    accent_color character varying(7) DEFAULT ''::character varying NOT NULL,
    contact_email character varying(255) DEFAULT ''::character varying NOT NULL,
    contact_phone character varying(50) DEFAULT ''::character varying NOT NULL,
    footer_text text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone,
    loan_period_days integer,
    max_active_loans integer,
    max_renewals integer,
    block_when_overdue boolean,
    CONSTRAINT organization_settings_pkey PRIMARY KEY (id),
    CONSTRAINT organization_settings_singleton CHECK ((id = 1))
);


ALTER TABLE public.organization_settings OWNER TO postgres;

--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE public.users ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: activity_logs activity_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity_logs
    ADD CONSTRAINT activity_logs_pkey PRIMARY KEY (id);


--
-- Name: authors authors_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.authors
    ADD CONSTRAINT authors_pkey PRIMARY KEY (id);


--
-- Name: book_contents book_contents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_contents
    ADD CONSTRAINT book_contents_pkey PRIMARY KEY (id);


--
-- Name: book_copies book_copies_copy_code_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_copies
    ADD CONSTRAINT book_copies_copy_code_key UNIQUE (copy_code);


--
-- Name: book_copies book_copies_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_copies
    ADD CONSTRAINT book_copies_pkey PRIMARY KEY (id);


--
-- Name: book_volumes book_volumes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_volumes
    ADD CONSTRAINT book_volumes_pkey PRIMARY KEY (id);


--
-- Name: books books_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.books
    ADD CONSTRAINT books_pkey PRIMARY KEY (id);


--
-- Name: borrowers borrowers_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.borrowers
    ADD CONSTRAINT borrowers_pkey PRIMARY KEY (id);


--
-- Name: categories categories_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_name_key UNIQUE (name);


--
-- Name: categories categories_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.categories
    ADD CONSTRAINT categories_pkey PRIMARY KEY (id);


--
-- Name: loans loans_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.loans
    ADD CONSTRAINT loans_pkey PRIMARY KEY (id);


--
-- Name: locations locations_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT locations_name_key UNIQUE (name);


--
-- Name: locations locations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT locations_pkey PRIMARY KEY (id);


--
-- Name: publishers publishers_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.publishers
    ADD CONSTRAINT publishers_pkey PRIMARY KEY (id);


--
-- Name: shelves shelves_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shelves
    ADD CONSTRAINT shelves_pkey PRIMARY KEY (id);


--
-- Name: authors unique_author_name; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.authors
    ADD CONSTRAINT unique_author_name UNIQUE (name);


--
-- Name: book_volumes unique_book_volume; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_volumes
    ADD CONSTRAINT unique_book_volume UNIQUE (book_id, volume_number);


--
-- Name: shelves unique_shelf_per_location; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shelves
    ADD CONSTRAINT unique_shelf_per_location UNIQUE (location_id, shelf_code);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: idx_activity_logs_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_activity_logs_created_at ON public.activity_logs USING btree (created_at DESC);


--
-- Name: idx_activity_logs_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_activity_logs_user_id ON public.activity_logs USING btree (user_id);


--
-- Name: idx_authors_name_trgm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_authors_name_trgm ON public.authors USING gin (name public.gin_trgm_ops);


--
-- Name: idx_book_contents_parent_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_book_contents_parent_id ON public.book_contents USING btree (parent_id);


--
-- Name: idx_book_contents_title; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_book_contents_title ON public.book_contents USING btree (title);


--
-- Name: idx_book_contents_title_trgm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_book_contents_title_trgm ON public.book_contents USING gin (title public.gin_trgm_ops);


--
-- Name: idx_book_contents_volume_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_book_contents_volume_id ON public.book_contents USING btree (volume_id);


--
-- Name: idx_book_copies_shelf_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_book_copies_shelf_id ON public.book_copies USING btree (shelf_id);


--
-- Name: idx_book_copies_volume_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_book_copies_volume_id ON public.book_copies USING btree (volume_id);


--
-- Name: idx_books_author_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_books_author_id ON public.books USING btree (author_id);


--
-- Name: idx_books_archived_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_books_archived_at ON public.books USING btree (archived_at) WHERE (archived_at IS NOT NULL);


--
-- Name: idx_books_category_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_books_category_id ON public.books USING btree (category_id);


--
-- Name: idx_books_publisher_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_books_publisher_id ON public.books USING btree (publisher_id);


--
-- Name: idx_books_title; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_books_title ON public.books USING btree (title);


--
-- Name: idx_books_title_trgm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_books_title_trgm ON public.books USING gin (title public.gin_trgm_ops);


--
-- Name: idx_borrowers_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_borrowers_name ON public.borrowers USING btree (name);


--
-- Name: idx_borrowers_name_trgm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_borrowers_name_trgm ON public.borrowers USING gin (name public.gin_trgm_ops);


--
-- Name: idx_borrowers_phone; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_borrowers_phone ON public.borrowers USING btree (phone);


--
-- Name: idx_loans_active_due; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_loans_active_due ON public.loans USING btree (due_date) WHERE (return_date IS NULL);


--
-- Name: idx_loans_borrower; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_loans_borrower ON public.loans USING btree (borrower_id);


--
-- Name: idx_loans_copy; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_loans_copy ON public.loans USING btree (copy_id);


--
-- Name: idx_loans_issued_by; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_loans_issued_by ON public.loans USING btree (issued_by);


--
-- Name: idx_shelves_shelf_code; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX idx_shelves_shelf_code ON public.shelves USING btree (shelf_code);


--
-- Name: unique_active_loan_per_copy; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX unique_active_loan_per_copy ON public.loans USING btree (copy_id) WHERE (return_date IS NULL);


--
-- Name: unique_borrower_registration_no; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX unique_borrower_registration_no ON public.borrowers USING btree (registration_no) WHERE ((registration_no IS NOT NULL) AND (btrim((registration_no)::text) <> ''::text));


--
-- Name: unique_publisher_name; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX unique_publisher_name ON public.publishers USING btree (name);


--
-- Name: activity_logs fk_activity_user; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.activity_logs
    ADD CONSTRAINT fk_activity_user FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: books fk_books_author; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.books
    ADD CONSTRAINT fk_books_author FOREIGN KEY (author_id) REFERENCES public.authors(id);


--
-- Name: books fk_books_category; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.books
    ADD CONSTRAINT fk_books_category FOREIGN KEY (category_id) REFERENCES public.categories(id);


--
-- Name: books fk_books_publisher; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.books
    ADD CONSTRAINT fk_books_publisher FOREIGN KEY (publisher_id) REFERENCES public.publishers(id);


--
-- Name: book_contents fk_contents_parent; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_contents
    ADD CONSTRAINT fk_contents_parent FOREIGN KEY (parent_id) REFERENCES public.book_contents(id) ON DELETE CASCADE;


--
-- Name: book_contents fk_contents_volume; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_contents
    ADD CONSTRAINT fk_contents_volume FOREIGN KEY (volume_id) REFERENCES public.book_volumes(id) ON DELETE CASCADE;


--
-- Name: book_copies fk_copies_shelf; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_copies
    ADD CONSTRAINT fk_copies_shelf FOREIGN KEY (shelf_id) REFERENCES public.shelves(id);


--
-- Name: book_copies fk_copies_volume; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_copies
    ADD CONSTRAINT fk_copies_volume FOREIGN KEY (volume_id) REFERENCES public.book_volumes(id);


--
-- Name: loans fk_loans_borrower; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.loans
    ADD CONSTRAINT fk_loans_borrower FOREIGN KEY (borrower_id) REFERENCES public.borrowers(id);


--
-- Name: loans fk_loans_copy; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.loans
    ADD CONSTRAINT fk_loans_copy FOREIGN KEY (copy_id) REFERENCES public.book_copies(id);


--
-- Name: loans fk_loans_issued_by; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.loans
    ADD CONSTRAINT fk_loans_issued_by FOREIGN KEY (issued_by) REFERENCES public.users(id);


--
-- Name: loans fk_loans_returned_to; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.loans
    ADD CONSTRAINT fk_loans_returned_to FOREIGN KEY (returned_to) REFERENCES public.users(id);


--
-- Name: shelves fk_shelves_location; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.shelves
    ADD CONSTRAINT fk_shelves_location FOREIGN KEY (location_id) REFERENCES public.locations(id) ON DELETE CASCADE;


--
-- Name: book_volumes fk_volumes_book; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.book_volumes
    ADD CONSTRAINT fk_volumes_book FOREIGN KEY (book_id) REFERENCES public.books(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict eTUL6e4u3IQ8waGc0rWNRl670EvCK1kWYaYTyEbzAeMsO4fEAiHFw79f9D7wjo1



--
-- Name: inventory_sessions; Type: TABLE; Schema: public; Owner: postgres
--
-- One physical stock check (see library.models.InventorySession). The
-- CHECKs are the rules: a session covers something coherent, and it is
-- Completed exactly when it has a completion time.
--

CREATE TABLE public.inventory_sessions (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name character varying(255) NOT NULL,
    scope character varying(20) NOT NULL,
    location_id integer REFERENCES public.locations(id),
    shelf_id integer REFERENCES public.shelves(id),
    status character varying(20) DEFAULT 'In Progress'::character varying NOT NULL,
    started_by integer REFERENCES public.users(id),
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,

    CONSTRAINT check_inventory_scope CHECK (
        (scope = 'library' AND location_id IS NULL AND shelf_id IS NULL)
        OR (scope = 'location' AND location_id IS NOT NULL AND shelf_id IS NULL)
        OR (scope = 'shelf' AND shelf_id IS NOT NULL AND location_id IS NULL)
    ),

    CONSTRAINT check_inventory_status CHECK (
        (status = 'In Progress' AND completed_at IS NULL)
        OR (status = 'Completed' AND completed_at IS NOT NULL)
    )
);


ALTER TABLE public.inventory_sessions OWNER TO postgres;


--
-- Name: inventory_scans; Type: TABLE; Schema: public; Owner: postgres
--
-- Every code read during a stock check, successful or not (see
-- library.models.InventoryScan). `unique_found_copy_per_session` below is
-- what stops a repeated scan raising the Found count.
--

CREATE TABLE public.inventory_scans (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id integer NOT NULL
        REFERENCES public.inventory_sessions(id) ON DELETE CASCADE,
    copy_id integer REFERENCES public.book_copies(id),
    copy_code character varying(50) NOT NULL,
    outcome character varying(20) NOT NULL,
    scanned_by integer REFERENCES public.users(id),
    scanned_at timestamp with time zone NOT NULL,

    CONSTRAINT check_inventory_scan_outcome CHECK (
        outcome IN ('found', 'duplicate', 'outside', 'unknown')
    ),

    CONSTRAINT check_inventory_scan_copy CHECK (
        (copy_id IS NOT NULL) OR (outcome = 'unknown')
    )
);


ALTER TABLE public.inventory_scans OWNER TO postgres;


CREATE UNIQUE INDEX unique_found_copy_per_session
    ON public.inventory_scans (session_id, copy_id)
    WHERE outcome = 'found';


CREATE INDEX idx_inventory_scans_session
    ON public.inventory_scans (session_id);


--
-- Name: reservations; Type: TABLE; Schema: public; Owner: postgres
--
-- A borrower waiting for a book, in the order they asked (see
-- library.models.Reservation). `unique_active_reservation` below is what
-- stops one borrower holding two places in the same queue.
--

CREATE TABLE public.reservations (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    borrower_id integer NOT NULL REFERENCES public.borrowers(id),
    book_id integer NOT NULL REFERENCES public.books(id),
    status character varying(20) DEFAULT 'Active'::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    closed_at timestamp with time zone,

    CONSTRAINT check_reservation_status CHECK (
        status IN ('Active', 'Fulfilled', 'Cancelled')
    ),

    CONSTRAINT check_reservation_closed CHECK (
        (status = 'Active' AND closed_at IS NULL)
        OR (status <> 'Active' AND closed_at IS NOT NULL)
    )
);


ALTER TABLE public.reservations OWNER TO postgres;


CREATE UNIQUE INDEX unique_active_reservation
    ON public.reservations (borrower_id, book_id)
    WHERE status = 'Active';


CREATE INDEX idx_reservations_book_queue
    ON public.reservations (book_id, created_at, id)
    WHERE status = 'Active';


CREATE INDEX idx_reservations_borrower
    ON public.reservations (borrower_id, created_at);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: postgres
--
-- One actionable message for one member of staff (see
-- library.models.Notification). Everything it displays is written into the
-- row, so it keeps saying what it said after the record that caused it
-- changes state. `unique_notification_event` below is what makes creation
-- idempotent: one row per recipient per transition, whatever a refresh, a
-- retry or two simultaneous requests do.
--

CREATE TABLE public.notifications (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient_id integer NOT NULL REFERENCES public.users(id),
    event_type character varying(50) NOT NULL,
    event_key character varying(200) NOT NULL,
    title character varying(255) NOT NULL,
    message text DEFAULT ''::text NOT NULL,
    url character varying(500) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone NOT NULL,
    read_at timestamp with time zone,

    CONSTRAINT check_notification_event_type CHECK (
        event_type IN ('reservation_ready', 'stock_check_missing')
    )
);


ALTER TABLE public.notifications OWNER TO postgres;


CREATE UNIQUE INDEX unique_notification_event
    ON public.notifications (recipient_id, event_key);


CREATE INDEX idx_notifications_recipient_newest
    ON public.notifications (recipient_id, created_at DESC, id DESC);


CREATE INDEX idx_notifications_unread
    ON public.notifications (recipient_id, created_at DESC, id DESC)
    WHERE read_at IS NULL;
