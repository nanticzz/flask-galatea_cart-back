from flask import Blueprint, render_template, current_app, g, url_for, \
    flash, redirect, session, request
from galatea.tryton import tryton
from flask.ext.babel import gettext as _
from flask.ext.wtf import Form
from wtforms import TextField, SelectField, IntegerField, validators
from decimal import Decimal
from emailvalid import check_email
import vatnumber

cart = Blueprint('cart', __name__, template_folder='templates')

shop = current_app.config.get('TRYTON_SALE_SHOP')
shops = current_app.config.get('TRYTON_SALE_SHOPS')

Cart = tryton.pool.get('sale.cart')
Line = tryton.pool.get('sale.line')
Product = tryton.pool.get('product.product')
Address = tryton.pool.get('party.address')
Shop = tryton.pool.get('sale.shop')
Carrier = tryton.pool.get('carrier')
Party = tryton.pool.get('party.party')
Address = tryton.pool.get('party.address')
Sale = tryton.pool.get('sale.sale')
SaleLine = tryton.pool.get('sale.line')

CART_FIELD_NAMES = [
    'cart_date', 'product_id', 'product.rec_name', 'product.template.esale_slug',
    'quantity', 'unit_price', 'untaxed_amount', 'total_amount',
    ]
CART_ORDER = [
    ('cart_date', 'DESC'),
    ('id', 'DESC'),
    ]

VAT_COUNTRIES = [('', '')]
for country in vatnumber.countries():
    VAT_COUNTRIES.append((country, country))

class AddressForm(Form):
    "Address form"
    name = TextField(_('Name'), [validators.Required()])
    street = TextField(_('Street'), [validators.Required()])
    city = TextField(_('City'), [validators.Required()])
    zip = TextField(_('Zip'), [validators.Required()])
    country = SelectField(_('Country'), [validators.Required(), ], coerce=int)
    subdivision = IntegerField(_('State/County'), [validators.Required()])
    email = TextField(_('Email'), [validators.Required(), validators.Email()])
    phone = TextField(_('Phone'))
    vat_country = SelectField(_('VAT Country'), [validators.Required(), ])
    vat_number = TextField(_('VAT Number'), [validators.Required()])

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True


@cart.route("/confirm/", methods=["POST"], endpoint="confirm")
@tryton.transaction()
def confirm(lang):
    '''Convert carts to sale order
    Return to Sale Details
    '''
    sshop = Shop(shop)
    data = request.form

    party = session.get('customer')
    shipment_address = data.get('shipment_address')
    name = data.get('name')
    email = data.get('email')

    # Get all carts
    domain = [
        ('state', '=', 'draft'),
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search(domain)
    if not carts:
        flash(_('There are not products in your cart.'), 'danger')
        return redirect(url_for('.cart', lang=g.language))

    # New party
    if party:
        party = Party(party)
    else:
        if not check_email(email):
            flash(_('Email "{email}" not valid.').format(
                email=email), 'danger')
            return redirect(url_for('.cart', lang=g.language))

        party = Party.esale_create_party(sshop, {
            'name': name,
            'esale_email': email,
            'vat_country': data.get('vat_country', None),
            'vat_number': data.get('vat_number', None),
            })
        session['customer'] = party.id

    if shipment_address != 'new-address':
        address = Address(shipment_address)
    else:
        country = None
        if data.get('country'):
            country = int(data.get('country'))
        subdivision = None
        if data.get('subdivision'):
            subdivision = int(data.get('subdivision'))

        values = {
            'name': name,
            'street': data.get('street'),
            'city': data.get('city'),
            'zip': data.get('zip'),
            'country': country,
            'subdivision': subdivision,
            'phone': data.get('phone'),
            'email': email,
            'fax': None,
            }
        address = Address.esale_create_address(sshop, party, values)

    # Carts are same party to create a new sale
    Cart.write(carts, {'party': party})

    # Create new sale
    values = {}
    values['shipment_cost_method'] = 'order' # force shipment invoice on order
    values['shipment_address'] = address
    payment_type = data.get('payment_type')
    if payment_type:
        values['payment_type'] = int(payment_type)
    carrier = data.get('carrier')
    if carrier:
        values['carrier'] = int(carrier)
    comment = data.get('comment')
    if comment:
        values['comment'] = comment

    sales = Cart.create_sale(carts, values)
    if not sales:
        current_app.logger.error('Sale. Error create sale party %s' % party.id)
        flash(_('It has not been able to convert to cart into an order. ' \
            'Try again or contact us'), 'danger')
        return redirect(url_for('.cart', lang=g.language))
    sale, = sales

    # Add shipment line
    product = sshop.esale_delivery_product
    shipment_price = Decimal(data.get('carrier-cost'))
    shipment_line = SaleLine.get_shipment_line(product, shipment_price, sale)
    shipment_line.save()

    # sale draft to quotation
    Sale.quote([sale])

    if current_app.debug:
        current_app.logger.info('Sale. Create sale %s' % sale.id)

    flash(_('Created sale order successfully.'), 'success')

    return redirect(url_for('sale.sale', lang=g.language, id=sale.id))


@cart.route("/add/", methods=["POST"], endpoint="add")
@tryton.transaction()
def add(lang):
    '''Add product item cart'''
    to_create = []
    to_update = []
    to_remove = []
    to_remove_products = [] # Products in older cart and don't sell

    # Convert form values to dict values {'id': 'qty'}
    values = {}
    for k, v in request.form.iteritems():
        product = k.split('-')
        if product[0] == 'product':
            try:
                values[int(product[1])] = float(v)
            except:
                flash(_('You try to add Qty not numeric. ' \
                    'Sorry, we do not continue.'))
                return redirect(url_for('.cart', lang=g.language))

    # Remove items in cart
    removes = request.form.getlist('remove')

    # Products Current User Cart (products to send)
    products_current_cart = [k for k,v in values.iteritems()]

    # Search current cart by user or session
    domain = [
        ('state', '=', 'draft'),
        ('product.id', 'in', products_current_cart)
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search(domain, order=[('cart_date', 'ASC')])

    # Products Current Cart (products available in sale.cart)
    products_in_cart = [c.product.id for c in carts]

    # Get product data
    products = Product.search_read([
        ('id', 'in', products_current_cart),
        ('template.esale_available', '=', True),
        ('template.esale_active', '=', True),
        ('template.esale_saleshops', 'in', shops),
        ], fields_names=['code', 'template.esale_price'])

    # Delete products data
    if removes:
        for remove in removes:
            for cart in carts:
                try:
                    if cart.id == int(remove):
                        to_remove.append(cart)
                        break
                except:
                    flash(_('You try to remove not numeric cart. Abort.'))
                    return redirect(url_for('.cart', lang=g.language))

    # Add/Update products data
    for product_id, qty in values.iteritems():
        # Get current product from products
        product_values = None
        for product in products:
            if product_id == product['id']:
                product_values = product
                break

        if not product_values and product_id in products_in_cart: # Remove products cart
            to_remove_products.append(product_id)
            continue

        # Create data
        if product_id not in products_in_cart and qty > 0:
            for product in products:
                if product['id'] == product_id:
                    to_create.append({
                        'party': session.get('customer', None),
                        'quantity': qty,
                        'product': product['id'],
                        'unit_price': product_values['template.esale_price'],
                        'sid': session.sid,
                        'galatea_user': session.get('user', None),
                    })

        # Update data
        if product_id in products_in_cart: 
            for cart in carts:
                if cart.product.id == product_id:
                    if qty > 0:
                        to_update.append({
                            'cart': cart,
                            'values': {
                                'quantity': qty,
                                'unit_price': product_values['template.esale_price'],
                                },
                            })
                    else: # Remove data when qty <= 0
                        to_remove.append(cart)
                    break

    # Add to remove older products
    if to_remove_products:
        for remove in to_remove_products:
            for cart in carts:
                if cart.product.id == remove:
                    to_remove.append(cart)
                    break

    # Add Cart
    if to_create:
        Cart.create(to_create)
        flash(_('Added {total} product/s in your cart.').format(
            total=len(to_create)), 'success')

    # Update Cart
    if to_update:
        for update in to_update:
            Cart.write([update['cart']], update['values'])
        total = len(to_update)
        if to_remove:
            total = total-len(to_remove)
        flash(_('Updated {total} product/s in your cart.').format(
            total=total), 'success')

    # Delete Cart
    if to_remove:
        Cart.delete(to_remove)
        flash(_('Deleted {total} product/s in your cart.').format(
            total=len(to_remove)), 'success')

    return redirect(url_for('.cart', lang=g.language))

@cart.route("/", endpoint="cart")
@tryton.transaction()
def cart_list(lang):
    '''Cart by user or session'''
    sshop = Shop(shop)

    form_address = AddressForm(
        country=sshop.esale_country.id,
        vat_country=sshop.esale_country.code)
    countries = [(c.id, c.name) for c in sshop.esale_countrys]
    form_address.country.choices = countries
    form_address.vat_country.choices = VAT_COUNTRIES

    domain = [
        ('state', '=', 'draft'),
        ]
    if session.get('user'): # login user
        domain.append(['OR', 
            ('sid', '=', session.sid),
            ('galatea_user', '=', session['user']),
            ])
    else: # anonymous user
        domain.append(
            ('sid', '=', session.sid),
            )
    carts = Cart.search_read(domain, order=CART_ORDER, fields_names=CART_FIELD_NAMES)

    addresses = None
    if session.get('customer'):
        addresses = Address.search([
            ("party", "=", session['customer']),
            ("active", "=", True),
            ], order=[('sequence', 'ASC'), ('id', 'ASC')])

    carriers = []
    for c in sshop.esale_carriers:
        carrier_id = c.id
        carrier = Carrier(carrier_id)
        price = carrier.get_sale_price()
        carriers.append({
            'id': carrier_id,
            'name': c.rec_name,
            'price': price[0]
            })

    #breadcumbs
    breadcrumbs = [{
        'slug': url_for('my-account', lang=g.language),
        'name': _('My Account'),
        }, {
        'slug': url_for('.cart', lang=g.language),
        'name': _('Cart'),
        }]

    return render_template('cart.html',
            breadcrumbs=breadcrumbs,
            shop=sshop,
            carts=carts,
            form_address=form_address,
            addresses=addresses,
            carriers=sorted(carriers, key=lambda k: k['price']),
            )
